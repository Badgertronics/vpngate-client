"""PyInstaller hook for gi.repository.Gdk (forcing GDK 4.0)."""
from PyInstaller.utils.hooks.gi import GiModuleInfo


def hook(hook_api):
    module_info = GiModuleInfo("Gdk", "4.0", hook_api=hook_api)
    if not module_info.available:
        return

    binaries, datas, hiddenimports = module_info.collect_typelib_data()
    hiddenimports += ["gi._gi_cairo", "gi.repository.cairo"]

    # On Windows, collect GdkWin32 typelib if available
    try:
        win32_info = GiModuleInfo("GdkWin32", "4.0", hook_api=hook_api)
        if win32_info.available:
            b, d, h = win32_info.collect_typelib_data()
            binaries += b
            datas += d
            hiddenimports += h
    except Exception:
        pass

    hook_api.add_datas(datas)
    hook_api.add_binaries(binaries)
    hook_api.add_imports(*hiddenimports)
