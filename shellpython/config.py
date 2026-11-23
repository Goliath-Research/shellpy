import pickle
import base64
import sys

# TODO: subject for further refactoring. Config should be serialized in nicer way. Now we cannot do it unless we break
# compatibility

# prints all commands being executed
PRINT_ALL_COMMANDS = False

# prints stdout of every command executed
PRINT_STDOUT_ALWAYS = False

# prints stderr of every command executed
PRINT_STDERR_ALWAYS = False

# colorama is a plugin that makes output colored, this flag controls whether it is enabled
COLORAMA_ENABLED = True

# maximum number of bytes kept from each output stream of a captured command, and the maximum length of a single line
# read in interactive mode. A command that exceeds it is killed and OutputLimitExceededError is raised
MAX_CAPTURED_STREAM_BYTES = 64 * 1024


def dumps():
    config_tuple = (PRINT_ALL_COMMANDS, PRINT_STDOUT_ALWAYS, PRINT_STDERR_ALWAYS, COLORAMA_ENABLED,
                    MAX_CAPTURED_STREAM_BYTES)
    serialized_config = pickle.dumps(config_tuple)

    if sys.version_info[0] == 2:
        return base64.b64encode(serialized_config)
    else:
        return str(base64.b64encode(serialized_config), 'utf-8')


def loads(data):
    global PRINT_ALL_COMMANDS, PRINT_STDOUT_ALWAYS, PRINT_STDERR_ALWAYS, COLORAMA_ENABLED, MAX_CAPTURED_STREAM_BYTES

    if sys.version_info[0] == 2:
        serialized_config = base64.b64decode(data)
    else:
        serialized_config = base64.b64decode(bytes(data, 'utf-8'))

    config_tuple = pickle.loads(serialized_config)

    PRINT_ALL_COMMANDS, PRINT_STDOUT_ALWAYS, PRINT_STDERR_ALWAYS, COLORAMA_ENABLED = config_tuple[:4]
    if len(config_tuple) > 4:
        MAX_CAPTURED_STREAM_BYTES = config_tuple[4]
