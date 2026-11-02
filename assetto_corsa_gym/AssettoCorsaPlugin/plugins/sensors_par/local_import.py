"""Import sibling modules from AC plugin dir or project package (Python 3.3)."""


def _has_package():
    pkg = globals().get('__package__')
    return pkg is not None and pkg != ''


def import_sibling(module_name):
    if _has_package():
        parent = __name__.rsplit('.', 1)[0]
        if parent and parent != __name__:
            try:
                return __import__(parent + '.' + module_name, globals(), locals(), [module_name], 0)
            except (ImportError, SystemError):
                pass
    return __import__(module_name)
