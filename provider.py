# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider
from .algoritmo import GerarNascentes
from .algoritmo_app import GerarAPP


class RestaureProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(GerarNascentes())
        self.addAlgorithm(GerarAPP())

    def id(self):
        return "restaure"

    def name(self):
        return "Restaure"

    def longName(self):
        return self.name()

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "docs", "images", "icon.png"))
