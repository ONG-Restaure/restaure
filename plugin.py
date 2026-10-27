# -*- coding: utf-8 -*-
from qgis.core import QgsApplication
from .provider import RestaureProvider


class RestaurePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initProcessing(self):
        self.provider = RestaureProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()

    def unload(self):
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
