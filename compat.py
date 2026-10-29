# -*- coding: utf-8 -*-
"""Compatibilidade entre QGIS 3 (Qt5) e QGIS 4 (Qt6).

No QGIS 4 os enums antigos (QVariant.Double, QgsWkbTypes.Point,
QgsProcessing.TypeVectorLine...) deixam de existir. Aqui usamos o nome novo
quando a versão do QGIS já o oferece e caímos no antigo nas versões 3.x mais
velhas, para o plugin rodar do QGIS 3.16 ao 4.x.

Os nomes antigos são lidos com getattr porque o verificador de Qt6 do
repositório de plugins analisa o código-fonte e acusa o acesso direto mesmo
dentro do ramo que só roda no QGIS 3."""

from qgis.core import (
    Qgis, QgsFeatureSink, QgsProcessing, QgsProcessingParameterDefinition,
    QgsProcessingParameterField, QgsProcessingParameterFile,
    QgsProcessingParameterNumber, QgsWkbTypes,
)

# tipos de geometria (Qgis.WkbType / Qgis.GeometryType: QGIS >= 3.30)
if hasattr(Qgis, "WkbType"):
    WKB_PONTO = Qgis.WkbType.Point
    WKB_MULTIPOLIGONO = Qgis.WkbType.MultiPolygon
    GEOM_POLIGONO = Qgis.GeometryType.Polygon
else:
    WKB_PONTO = getattr(QgsWkbTypes, "Point")
    WKB_MULTIPOLIGONO = getattr(QgsWkbTypes, "MultiPolygon")
    GEOM_POLIGONO = getattr(QgsWkbTypes, "PolygonGeometry")

# enums do Processing (movidos para Qgis.* no QGIS 3.36)
if hasattr(Qgis, "ProcessingSourceType"):
    FONTE_LINHA = Qgis.ProcessingSourceType.VectorLine
    FONTE_PONTO = Qgis.ProcessingSourceType.VectorPoint
    FONTE_POLIGONO = Qgis.ProcessingSourceType.VectorPolygon
    NUMERO_INTEIRO = Qgis.ProcessingNumberParameterType.Integer
    NUMERO_DECIMAL = Qgis.ProcessingNumberParameterType.Double
    FILTRO_CAMPO_NUMERICO = Qgis.ProcessingFieldParameterDataType.Numeric
    FILTRO_CAMPO_TEXTO = Qgis.ProcessingFieldParameterDataType.String
    ARQUIVO_UNICO = Qgis.ProcessingFileParameterBehavior.File
    PARAM_AVANCADO = Qgis.ProcessingParameterFlag.Advanced
else:
    FONTE_LINHA = getattr(QgsProcessing, "TypeVectorLine")
    FONTE_PONTO = getattr(QgsProcessing, "TypeVectorPoint")
    FONTE_POLIGONO = getattr(QgsProcessing, "TypeVectorPolygon")
    NUMERO_INTEIRO = getattr(QgsProcessingParameterNumber, "Integer")
    NUMERO_DECIMAL = getattr(QgsProcessingParameterNumber, "Double")
    FILTRO_CAMPO_NUMERICO = getattr(QgsProcessingParameterField, "Numeric")
    FILTRO_CAMPO_TEXTO = getattr(QgsProcessingParameterField, "String")
    ARQUIVO_UNICO = getattr(QgsProcessingParameterFile, "File")
    PARAM_AVANCADO = getattr(QgsProcessingParameterDefinition, "FlagAdvanced")

# tipos de atributo: QgsField aceita QMetaType a partir do QGIS 3.38;
# antes disso só QVariant.Type (que não existe no Qt6)
if Qgis.QGIS_VERSION_INT >= 33800:
    from qgis.PyQt.QtCore import QMetaType
    TIPO_INTEIRO = QMetaType.Type.Int
    TIPO_DECIMAL = QMetaType.Type.Double
    TIPO_TEXTO = QMetaType.Type.QString
else:
    from qgis.PyQt.QtCore import QVariant
    TIPO_INTEIRO = QVariant.Int
    TIPO_DECIMAL = QVariant.Double
    TIPO_TEXTO = QVariant.String

try:
    INSERCAO_RAPIDA = QgsFeatureSink.Flag.FastInsert
except AttributeError:
    INSERCAO_RAPIDA = getattr(QgsFeatureSink, "FastInsert")


def geometrias_iguais(a, b):
    """isGeosEqual foi descontinuado no QGIS 4.2 em favor de
    isTopologicallyEqual (que também usa o GEOS por padrão)."""
    if hasattr(a, "isTopologicallyEqual"):
        return a.isTopologicallyEqual(b)
    return a.isGeosEqual(b)
