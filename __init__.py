# -*- coding: utf-8 -*-
def classFactory(iface):
    from .plugin import RestaurePlugin
    return RestaurePlugin(iface)
