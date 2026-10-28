"""PyInstaller hook for gi.repository.Adw (Libadwaita 1)."""
from PyInstaller.utils.hooks import get_hook_config
from PyInstaller.utils.hooks.gi import GiModuleInfo, collect_glib_translations


def hook(hook_api):
    module_info = GiModuleInfo("Adw", "1", hook_api=hook_api)
    if not module_info.available:
        return

    binaries, datas, hiddenimports = module_info.collect_typelib_data()

    lang_list = get_hook_config(hook_api, "gi", "languages")
    datas += collect_glib_translations("libadwaita", lang_list)

    hook_api.add_datas(datas)
    hook_api.add_binaries(binaries)
    hook_api.add_imports(*hiddenimports)
