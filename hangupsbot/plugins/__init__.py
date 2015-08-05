import os
import sys
import logging
import inspect

from inspect import getmembers, isfunction
from commands import command
import handlers


logger = logging.getLogger(__name__)


class tracker:
    """used by the plugin loader to keep track of loaded commands
    designed to accommodate the dual command registration model (via function or decorator)
    registration might be called repeatedly depending on the "path" a command registration takes
    """
    def __init__(self):
        self.bot = None
        self.list = []
        self.reset()

    def set_bot(self, bot):
        self.bot = bot

    def reset(self):
        self._current = {
            "commands": {
                "admin": [],
                "user": [],
                "all": None,
                "tagged": {}
            },
            "handlers": [],
            "shared": [],
            "metadata": None
        }

    def start(self, metadata):
        self.reset()
        self._current["metadata"] = metadata

    def current(self):
        self._current["commands"]["all"] = list(
            set(self._current["commands"]["admin"] +
                self._current["commands"]["user"]))
        return self._current

    def end(self):
        self.list.append(self.current())

        # sync tagged commands to the command dispatcher
        if self._current["commands"]["tagged"]:
            for command_name, command_tagsets in self._current["commands"]["tagged"].items():
                command.register_tags(command_name, command_tagsets)

    def register_command(self, type, command_names, tags=None):
        """call during plugin init to register commands"""
        self._current["commands"][type].extend(command_names)
        self._current["commands"][type] = list(set(self._current["commands"][type]))

        self.register_tags(command_names, tags)

    def register_tags(self, command_names, tags=None):
        config_plugins_tags_autoregister = self.bot.get_config_option('plugins.tags.auto-register')
        if config_plugins_tags_autoregister is None:
            config_plugins_tags_autoregister = True

        if tags:
            if isinstance(tags, str):
                tags = [tags]
        else:
            if not config_plugins_tags_autoregister:
                return
            # even if no tags are explicitly specified, we need to auto-register system tags

        for command in command_names:
            if command not in self._current["commands"]["tagged"]:
                self._current["commands"]["tagged"][command] = set()

            base = tags
            if config_plugins_tags_autoregister:
                if config_plugins_tags_autoregister is True:
                    _tag = "execute-{}"
                else:
                    _tag = config_plugins_tags_autoregister

                if not base:
                    base = []
                base += [ _tag.format(command) ]

            tagsets = set([ frozenset(item if isinstance(item, list) else [item]) for item in base ])

            # registration might be called repeatedly, so only add the tagsets if it doesnt exist
            if tagsets > self._current["commands"]["tagged"][command]:
                self._current["commands"]["tagged"][command] = self._current["commands"]["tagged"][command] | tagsets
                logger.debug("{} - tags: {}".format(command, tagsets))

    def register_handler(self, function, type, priority):
        self._current["handlers"].append((function, type, priority))

    def register_shared(self, id, objectref, forgiving):
        self._current["shared"].append((id, objectref, forgiving))


tracking = tracker()


"""helpers, used by loaded plugins to register commands"""


def register_user_command(command_names, tags=None):
    """user command registration"""
    if not isinstance(command_names, list):
        command_names = [command_names]
    tracking.register_command("user", command_names, tags=tags)

def register_admin_command(command_names, tags=None):
    """admin command registration, overrides user command registration"""
    if not isinstance(command_names, list):
        command_names = [command_names]
    tracking.register_command("admin", command_names, tags=tags)

def register_handler(function, type="message", priority=50):
    """register external handler"""
    bot_handlers = tracking.bot._handlers
    bot_handlers.register_handler(function, type, priority)

def register_shared(id, objectref, forgiving=True):
    """register shared object"""
    bot = tracking.bot
    bot.register_shared(id, objectref, forgiving=forgiving)


"""plugin loader"""


def retrieve_all_plugins(plugin_path=None, must_start_with=False):
    """recursively loads all plugins from the standard plugins path
    * a plugin file or folder must not begin with . or _
    * a subfolder containing a plugin must have an __init__.py file
    * sub-plugin files (additional plugins inside a subfolder) must be prefixed with the 
      plugin/folder name for it to be automatically loaded
    """

    if not plugin_path:
        plugin_path = os.path.dirname(os.path.realpath(sys.argv[0])) + os.sep + "plugins"

    plugin_list = []

    nodes = os.listdir(plugin_path)

    for node_name in nodes:
        full_path = os.path.join(plugin_path, node_name)
        module_names = [ os.path.splitext(node_name)[0] ] # node_name without .py extension

        if node_name.startswith(("_", ".")):
            continue

        if must_start_with and not node_name.startswith(must_start_with):
            continue

        if os.path.isfile(full_path):
            if not node_name.endswith(".py"):
                continue
        else:
            if not os.path.isfile(os.path.join(full_path, "__init__.py")):
                continue

            for sm in retrieve_all_plugins(full_path, must_start_with=node_name):
                module_names.append(module_names[0] + "." + sm)

        plugin_list.extend(module_names)

    logger.debug("retrieved {}: {}.{}".format(len(plugin_list), must_start_with or "plugins", plugin_list))
    return plugin_list


def get_configured_plugins(bot):
    all_plugins = retrieve_all_plugins()
    config_plugins = bot.get_config_option('plugins')

    if config_plugins is None: # must be unset in config or null
        logger.info("plugins is not defined, using ALL")
        plugin_list = all_plugins

    else:
        """perform fuzzy matching with actual retrieved plugins, e.g. "abc" matches "xyz.abc"
        if more than one match found, don't load plugin
        """
        plugins_included = []
        plugins_excluded = all_plugins

        plugin_name_ambiguous = []
        plugin_name_not_found = []

        for configured in config_plugins:
            dotconfigured = "." + configured

            matches = []
            for found in plugins_excluded:
                fullfound = "plugins." + found
                if fullfound.endswith(dotconfigured):
                    matches.append(found)
            num_matches = len(matches)

            if num_matches <= 0:
                logger.debug("{} no match".format(configured))
                plugin_name_not_found.append(configured)
            elif num_matches == 1:
                logger.debug("{} matched to {}".format(configured, matches[0]))
                plugins_included.append(matches[0])
                plugins_excluded.remove(matches[0])
            else:
                logger.debug("{} ambiguous, matches {}".format(configured, matches))
                plugin_name_ambiguous.append(configured)

        if plugins_excluded:
            logger.info("excluded {}: {}".format(len(plugins_excluded), plugins_excluded))

        if plugin_name_ambiguous:
            logger.warning("ambiguous plugin names: {}".format(plugin_name_ambiguous))

        if plugin_name_not_found:
            logger.warning("plugin not found: {}".format(plugin_name_not_found))

        plugin_list = plugins_included

    logger.info("included {}: {}".format(len(plugin_list), plugin_list))

    return plugin_list


def load_user_plugins(bot):
    """loads all user plugins"""

    plugin_list = get_configured_plugins(bot)

    for module in plugin_list:
        module_path = "plugins.{}".format(module)
        load(bot, module_path)


def load(bot, module_path, module_name=None):
    """loads a single plugin-like object as identified by module_path, and initialise it"""

    if module_name is None:
        module_name = module_path.split(".")[-1]

    tracking.start({ "module": module_name, "module.path": module_path })

    try:
        exec("import {}".format(module_path))
    except Exception as e:
        logger.exception("EXCEPTION during plugin import: {}".format(module_path))
        return

    public_functions = [o for o in getmembers(sys.modules[module_path], isfunction)]

    candidate_commands = []

    """pass 1: run optional callable: _initialise, _initialize
    * performs house-keeping tasks (e.g. migration, tear-up, pre-init, etc)
    * registers user and/or admin commands
    """
    available_commands = False # default: ALL
    try:
        for function_name, the_function in public_functions:
            if function_name ==  "_initialise" or function_name ==  "_initialize":
                """accepted function signatures:
                CURRENT
                version >= 2.4 | function()
                version >= 2.4 | function(bot) - parameter must be named "bot"
                LEGACY
                version <= 2.4 | function(handlers, bot)
                ancient        | function(handlers)
                """
                _expected = list(inspect.signature(the_function).parameters)
                if len(_expected) == 0:
                    the_function()
                    _return = []
                elif len(_expected) == 1 and _expected[0] == "bot":
                    the_function(bot)
                    _return = []
                else:
                    try:
                        # legacy support, pre-2.4
                        _return = the_function(bot._handlers, bot)
                    except TypeError as e:
                        # legacy support, ancient plugins
                        _return = the_function(bot._handlers)
                if type(_return) is list:
                    available_commands = _return
            elif function_name.startswith("_"):
                pass
            else:
                candidate_commands.append((function_name, the_function))
        if available_commands is False:
            # implicit init, legacy support: assume all candidate_commands are user-available
            register_user_command([function_name for function_name, function in candidate_commands])
        elif available_commands is []:
            # explicit init, no user-available commands
            pass
        else:
            # explicit init, legacy support: _initialise() returned user-available commands
            register_user_command(available_commands)
    except Exception as e:
        logger.exception("EXCEPTION during plugin init: {}".format(module_path))
        return # skip this, attempt next plugin

    """
    pass 2: register filtered functions
    tracking.current() and the CommandDispatcher registers might be out of sync if a 
    combination of decorators and register_user_command/register_admin_command is used since
    decorators execute immediately upon import
    """
    plugin_tracking = tracking.current()

    explicit_admin_commands = plugin_tracking["commands"]["admin"]
    all_commands = plugin_tracking["commands"]["all"]
    registered_commands = []
    for function_name, the_function in candidate_commands:
        if function_name in all_commands:
            is_admin = False
            text_function_name = function_name
            if function_name in explicit_admin_commands:
                is_admin = True
                text_function_name = "*" + text_function_name

            command.register(the_function, admin=is_admin, final=True)

            registered_commands.append(text_function_name)

    if registered_commands:
        logger.info("{} - {}".format(module_name, ", ".join(registered_commands)))
    else:
        logger.info("{} - no commands".format(module_name))

    tracking.end()

