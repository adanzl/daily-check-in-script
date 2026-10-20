import pkgutil as _pkgutil


class CheckIn:
    name = "Base"


__path__ = _pkgutil.extend_path(__path__, __name__)

# 这些模块不能在这里预先导入：
#   main / configs —— 本来就要按需加载
#   utils.db       —— 它带 __main__，被 walk_packages 提前 import 后再
#                     `python -m daily_checkin.utils.db` 会触发
#                     "found in sys.modules after import of package" 警告
_SKIP_IMPORT = (
    "daily_checkin.main",
    "daily_checkin.configs",
    "daily_checkin.utils.db",
)

for _, _modname, _ in _pkgutil.walk_packages(path=__path__, prefix=__name__ + "."):
    if _modname not in _SKIP_IMPORT:
        __import__(_modname)
