# -*- coding: utf-8 -*-

"""
ods_pro_generator
***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************

ODS PRO - Gerador de ODS do SIGEF em lote (ferramenta única).

Dois modos de geração:

- Lote a lote: gera UMA planilha ODS, da parcela selecionada na camada parcela.
- Em lote: gera uma planilha ODS para cada parcela, em sequência, a partir da
  parcela selecionada. Parcelas com ÁREA ENCRAVADA (anel interno) são geradas
  primeiro; em seguida as demais. Em cada grupo, a ordem é pela coluna
  "denominacao" (ordenação natural: Lote 2 antes de Lote 10).

Regras comuns aos dois modos:

- Para cada parcela, vértices e limites são selecionados POR LOCALIZAÇÃO
  (relação "toca"), sem depender do campo "lote".
- Parcelas multiparte geram UMA ODS com uma aba de perímetro por parte,
  ordenadas da MAIOR para a MENOR área: Parte 1 na aba 2, Parte 2 na aba 3...
  Em cada aba, B3 = "Parte N" e B4 = "00N".
- A geometria NÃO é validada/corrigida: auto-intersecção e partes que se
  tocam/sobrepõem são ignoradas.
"""

__author__ = 'Maik Rodrigues'
__date__ = '2026-10-08'
__copyright__ = '(C) 2024 by Maik Rodrigues'

import glob
import locale
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from math import floor, isnan
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication, QUrl
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    NULL,
    QgsCoordinateTransform,
    QgsDataSourceUri,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from ..compat import (
    FONTE_LINHA, FONTE_PONTO, FONTE_POLIGONO, LOG_AVISO, LOG_CRITICO,
    LOG_INFO, NUMERO_INTEIRO, SEM_THREAD,
)
from . import PASTA_PLUGIN


# ============================================================
# CONSTANTES
# ============================================================

CAMPO_DENOMINACAO = 'denominacao'

# A geração de ODS (captar vértice, limite e parcela) só roda com camadas
# provenientes deste banco de dados.
BANCO_PERMITIDO = 'GeoDataBase'

# Cota Z (campo "Z" da camada vertice): |Z| menor que este limite é
# considerado de baixa precisão/suspeito (ex.: 5, 1,5, 0,7, 0,25, 0,004).
LIMITE_BAIXA_PRECISAO = 10

# Casas decimais usadas para comparar coordenadas de vértices.
CASAS_CHAVE = 9

NATUREZA_SERVICO = {
    1: 'Particular',
    2: 'Contrato com Administração Pública'
}

TIPO_PESSOA = {1: 'Física', 2: 'Jurídica'}

SITUACAO = {
    1: 'Imóvel Registrado',
    2: 'Área Titulada não Registrada',
    3: 'Área não Titulada'
}

NATUREZA_AREA = {
    1: 'Assentamento', 2: 'Assentamento Parcela', 3: 'Estrada',
    4: 'Ferrovia', 5: 'Floresta Pública', 6: 'Gleba Pública',
    7: 'Particular', 8: 'Perímetro Urbano', 9: 'Terra Indígena',
    10: 'Terreno de Marinha', 11: 'Terreno Marginal',
    12: 'Território Quilombola', 13: 'Unidade de Conservação'
}

METODOS_POSICIONAMENTO = (
    'PG1', 'PG2', 'PG3', 'PG4', 'PG5', 'PG6', 'PG7', 'PG8', 'PG9',
    'PT1', 'PT2', 'PT3', 'PT4', 'PT5', 'PT6', 'PT7', 'PT8', 'PT9',
    'PA1', 'PA2', 'PA3', 'PS1', 'PS2', 'PS3', 'PS4', 'PB1', 'PB2'
)

TIPOS_LIMITE = (
    'LA1', 'LA2', 'LA3', 'LA4', 'LA5', 'LA6', 'LA7',
    'LN1', 'LN2', 'LN3', 'LN4', 'LN5', 'LN6'
)


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def find_layer_by_name(layer_name):
    """Id da camada vetorial do projeto com esse nome (sem diferenciar maiúsculas)."""
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.name().lower() == layer_name.lower():
            return layer.id()
    return None


def nome_banco_da_camada(camada):
    """Nome do banco de dados de onde a camada é lida ('' se não identificável).

    - PostgreSQL/PostGIS: nome do banco (dbname da conexão).
    - Spatialite/GeoPackage/OGR em arquivo: nome do arquivo sem extensão.
    """
    provedor = camada.providerType()
    fonte = camada.source()

    if provedor == 'postgres':
        return (QgsDataSourceUri(fonte).database() or '').strip()

    if provedor == 'spatialite':
        caminho = QgsDataSourceUri(fonte).database() or ''
    else:
        caminho = fonte.split('|')[0]

    return Path(caminho).stem.strip() if caminho else ''


def validar_banco_geodatabase(camadas):
    """Aborta se alguma das camadas (parcela, vértice, limite) não vier do
    banco de dados BANCO_PERMITIDO."""
    for rotulo, camada in camadas:
        banco = nome_banco_da_camada(camada)
        if banco.casefold() != BANCO_PERMITIDO.casefold():
            raise QgsProcessingException(
                'Geração de ODS restrita ao banco de dados "{}". A camada {} '
                '("{}") pertence ao banco "{}". Carregue parcela, vértice e '
                'limite do banco "{}".'.format(
                    BANCO_PERMITIDO, rotulo, camada.name(),
                    banco or 'desconhecido', BANCO_PERMITIDO
                )
            )


def limpar_nome_arquivo(texto):
    """PA Amigos do Campo - Lote 009 -> PA_Amigos_do_Campo_Lote_009

    Sem acentos: PA Aliança - Lote 1 -> PA_Alianca_Lote_1
    """
    texto = str(texto).strip()
    # Remove acentos e cedilha: ç -> c, ã -> a, é -> e ...
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"\s*[-–—]\s*", " ", texto)
    texto = re.sub(r'[<>:"/\\|?*,]', "_", texto)
    texto = re.sub(r"\s+", "_", texto)
    texto = re.sub(r"_e_", "_", texto)
    texto = re.sub(r"_+", "_", texto)
    return texto.strip("._") or "parcela"


def chave_natural(valor):
    """Ordenação natural: Lote 2 antes de Lote 10."""
    texto = str(valor or "").strip().casefold()
    chave = []
    for parte in re.split(r"(\d+)", texto):
        if parte.isdigit():
            chave.append((0, int(parte)))
        else:
            chave.append((1, parte))
    return chave


def obter_caminho_livre(pasta_saida, nome_base):
    """Caminho .ods que ainda não existe (nome, nome_2, nome_3...)."""
    caminho = os.path.join(pasta_saida, nome_base + ".ods")
    contador = 2
    while os.path.exists(caminho):
        caminho = os.path.join(pasta_saida, "{}_{}.ods".format(nome_base, contador))
        contador += 1
    return caminho


def geometria_no_crs_da_camada(geometria, crs_origem, crs_destino):
    """Transforma a geometria da parcela para o SRC da camada alvo."""
    geom = QgsGeometry(geometria)
    if crs_origem != crs_destino:
        geom.transform(
            QgsCoordinateTransform(crs_origem, crs_destino, QgsProject.instance())
        )
    return geom


def chave_ponto(x, y):
    """Chave de coordenada usada para comparar vértices."""
    return (round(float(x), CASAS_CHAVE), round(float(y), CASAS_CHAVE))


def partes_do_poligono(geom):
    """Partes do polígono, SEM validar/corrigir a geometria.

    Cada parte é uma lista de anéis; cada anel, uma lista de QgsPointXY.
    """
    if geom is None or geom.isNull() or geom.isEmpty():
        return []

    if geom.isMultipart():
        partes = geom.asMultiPolygon()
    else:
        partes = [geom.asPolygon()]

    return [p for p in partes if p and len(p[0]) >= 4]


def area_anel(anel):
    """Área absoluta do anel (fórmula de Gauss). Não depende de topologia."""
    soma = 0.0

    for a, b in zip(anel[:-1], anel[1:]):
        soma += a.x() * b.y() - b.x() * a.y()

    if anel and (anel[0].x() != anel[-1].x() or anel[0].y() != anel[-1].y()):
        soma += anel[-1].x() * anel[0].y() - anel[0].x() * anel[-1].y()

    return abs(soma) / 2.0


def partes_ordenadas_por_area(geom):
    """Partes do polígono da MAIOR para a MENOR área (anel externo)."""
    partes = partes_do_poligono(geom)
    return sorted(partes, key=lambda parte: area_anel(parte[0]), reverse=True)


def tem_area_encravada(geom):
    """True se a parcela possui ÁREA ENCRAVADA (anel interno/buraco em alguma parte).

    Não depende de topologia: apenas conta os anéis de cada parte.
    """
    return any(len(parte) > 1 for parte in partes_do_poligono(geom))


def _nome_tipo_geometria(geom):
    try:
        return QgsWkbTypes.displayString(geom.wkbType()) or ''
    except Exception:
        return ''


def linhas_da_geometria(geom):
    """Polilinhas de LineString/MultiLineString ([] para outros tipos, ex.: PointZ)."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []

    nome = _nome_tipo_geometria(geom)
    if 'Line' not in nome and 'Curve' not in nome:
        return []

    try:
        if geom.isMultipart():
            linhas = geom.asMultiPolyline()
        else:
            linhas = [geom.asPolyline()]
    except Exception:
        return []

    return [l for l in linhas if len(l) >= 2]


def pontos_da_geometria(geom):
    """Pontos de Point/MultiPoint ([] para outros tipos)."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []

    if 'Point' not in _nome_tipo_geometria(geom):
        return []

    try:
        if geom.isMultipart():
            return list(geom.asMultiPoint())
        return [geom.asPoint()]
    except Exception:
        return []


class AlvoParcela:
    """Parcela usada como alvo da seleção por localização.

    Geometria válida: relação "TOCA (touches)". Geometria inválida
    (auto-intersecção, partes sobrepostas): verificação pelo CONTORNO.
    """

    def __init__(self, geom, crs):
        self.geom = geom
        try:
            self.valida = bool(geom.isGeosValid())
        except Exception:
            self.valida = False

        self.chaves = set()
        for v in geom.vertices():
            self.chaves.add(chave_ponto(v.x(), v.y()))

        self.tol = 1e-9 if crs.isGeographic() else 1e-4

        contornos = []
        for parte in partes_do_poligono(geom):
            for anel in parte:
                contornos.append(QgsGeometry.fromPolylineXY(anel))

        self.contorno = (
            QgsGeometry.collectGeometry(contornos) if contornos else None
        )

    def ponto_no_contorno(self, p):
        if chave_ponto(p.x(), p.y()) in self.chaves:
            return True

        if self.contorno is None:
            return False

        return self.contorno.distance(QgsGeometry.fromPointXY(p)) <= self.tol

    def toca(self, geom):
        if self.valida:
            try:
                return bool(geom.touches(self.geom))
            except Exception:
                pass

        linhas = linhas_da_geometria(geom)

        if linhas:
            for linha in linhas:
                anterior = None
                for p in linha:
                    if not self.ponto_no_contorno(p):
                        return False
                    if anterior is not None:
                        meio = QgsPointXY(
                            (p.x() + anterior.x()) / 2.0,
                            (p.y() + anterior.y()) / 2.0
                        )
                        if not self.ponto_no_contorno(meio):
                            return False
                    anterior = p
            return True

        pontos = pontos_da_geometria(geom)

        if pontos:
            return any(self.ponto_no_contorno(p) for p in pontos)

        return False


def feicoes_por_localizacao(camada, geometria_parcela, crs_parcela):
    """Feições da camada que TOCAM a parcela (não altera a seleção da camada)."""
    if camada is None or not camada.isValid():
        raise QgsProcessingException(
            "A camada informada para seleção por localização é inválida."
        )

    if geometria_parcela is None or geometria_parcela.isEmpty():
        raise QgsProcessingException("A parcela possui geometria vazia.")

    geom_alvo = geometria_no_crs_da_camada(
        geometria_parcela, crs_parcela, camada.crs()
    )

    alvo = AlvoParcela(geom_alvo, camada.crs())

    retangulo = geom_alvo.boundingBox()
    retangulo.grow(alvo.tol * 10)

    return [
        feat for feat in camada.getFeatures(QgsFeatureRequest().setFilterRect(retangulo))
        if feat.hasGeometry() and not feat.geometry().isEmpty()
        and alvo.toca(feat.geometry())
    ]


def selecionar_por_localizacao(camada, geometria_parcela, crs_parcela):
    """Seleciona na camada as feições que TOCAM a parcela e devolve os ids."""
    ids = [
        feat.id()
        for feat in feicoes_por_localizacao(camada, geometria_parcela, crs_parcela)
    ]
    camada.selectByIds(ids)
    return ids


# ---------------- atributos ----------------

def _eh_nulo(valor):
    if valor is None:
        return True
    try:
        if valor == NULL:
            return True
    except Exception:
        pass
    return False


def attr_texto(feat, nome):
    """Atributo como texto limpo ('' quando nulo ou inexistente)."""
    if feat.fieldNameIndex(nome) < 0:
        return ''
    valor = feat[nome]
    if _eh_nulo(valor):
        return ''
    return str(valor).strip().replace('NULL', '').replace('\n', '')


def attr_int(feat, nome):
    texto = attr_texto(feat, nome)
    if texto == '':
        return None
    try:
        return int(float(texto))
    except Exception:
        return None


def attr_float(feat, nome):
    if feat.fieldNameIndex(nome) < 0:
        return None
    valor = feat[nome]
    if _eh_nulo(valor):
        return None
    try:
        return float(valor)
    except Exception:
        return None


def escapar_macro(texto):
    """Escapa o texto para ficar dentro de aspas duplas no qgis_macro.py."""
    return (
        str(texto)
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\r', ' ')
        .replace('\n', ' ')
    )


def dd2dms(dd, n_digits=3):
    if dd != 0:
        graus = int(floor(abs(dd)))
        resto = round(abs(dd) - graus, 10)
        minutos = int(floor(60 * resto))
        resto = round(resto * 60 - minutos, 10)
        segundos = resto * 60
        if round(segundos, n_digits) == 60:
            minutos += 1
            segundos = 0
        if minutos == 60:
            graus += 1
            minutos = 0
        texto = '{:02d}'.format(graus) + ' '
        texto = texto + '{:02d}'.format(minutos) + ' '
        if n_digits < 1:
            texto = texto + '{:02d}'.format(int(segundos)) + ' '
        else:
            texto = texto + (
                '{:0' + str(3 + n_digits) + '.' + str(n_digits) + 'f}'
            ).format(segundos) + ' '
        return texto.replace('.', ',')

    texto = "00 00 " + (
        '{:0' + str(3 + n_digits) + '.' + str(n_digits) + 'f}'
    ).format(0)
    return texto.replace('.', ',')


def preparar_anel(anel):
    """Remove pontos repetidos, fecha o anel e inicia no ponto de maior latitude.

    Sem operações topológicas: funciona mesmo com auto-intersecção.
    """
    pts = []

    for p in anel:
        if pts and chave_ponto(p.x(), p.y()) == chave_ponto(
            pts[-1].x(), pts[-1].y()
        ):
            continue
        pts.append(p)

    if len(pts) > 1 and chave_ponto(pts[0].x(), pts[0].y()) != chave_ponto(
        pts[-1].x(), pts[-1].y()
    ):
        pts.append(pts[0])

    if len(pts) < 4:
        raise QgsProcessingException(
            "Anel do polígono com menos de 3 vértices distintos."
        )

    aberto = pts[:-1]
    inicio = max(range(len(aberto)), key=lambda i: aberto[i].y())

    return aberto[inicio:] + aberto[:inicio] + [aberto[inicio]]


def z_da_feicao(feat):
    try:
        absoluta = feat.geometry().constGet()
        if feat.geometry().isMultipart():
            absoluta = absoluta.geometryN(0)
        return float(absoluta.z())
    except Exception:
        return float('nan')


def validar_vertice(feat):
    id_feat = feat.id()
    codigo = attr_texto(feat, 'vertice')

    for campo in ('sigma_x', 'sigma_y', 'sigma_z'):
        valor = attr_float(feat, campo)
        if valor is None or not (0 <= valor <= 10):
            raise QgsProcessingException(
                'Erro no vértice {} ({}): o valor de "{}" ({}) está fora do '
                'intervalo (0 a 10) ou é nulo!'.format(id_feat, codigo, campo, valor)
            )

    metodo = attr_texto(feat, 'metodo_pos')
    if metodo not in METODOS_POSICIONAMENTO:
        raise QgsProcessingException(
            'Erro no vértice {} ({}): o "metodo_pos" ({}) é inválido! '
            'Verifique a lista de métodos permitidos.'.format(id_feat, codigo, metodo)
        )

    tipo_verti = attr_texto(feat, 'tipo_verti')
    if tipo_verti not in ('M', 'P', 'V'):
        raise QgsProcessingException(
            'Erro no vértice {} ({}): o "tipo_verti" ({}) é inválido! '
            'Deve ser "M", "P" ou "V".'.format(id_feat, codigo, tipo_verti)
        )

    if codigo == '':
        raise QgsProcessingException(
            'Erro no vértice {}: o atributo "vertice" está vazio ou nulo!'
            .format(id_feat)
        )

    if len(codigo) < 7:
        raise QgsProcessingException(
            'Erro no vértice {}: o "código do vértice" ("{}") deve ter ao '
            'menos 7 caracteres!'.format(id_feat, codigo)
        )


def validar_limite(feat):
    id_feat = feat.id()
    tipo = attr_texto(feat, 'tipo')

    if tipo not in TIPOS_LIMITE:
        raise QgsProcessingException(
            'Erro na feição limite {}: o atributo "tipo" ({}) é inválido! '
            'Verifique a lista de tipos permitidos.'.format(id_feat, tipo)
        )

    confrontan = attr_texto(feat, 'confrontan')
    if len(confrontan) < 3:
        raise QgsProcessingException(
            'Erro na feição limite {}: o atributo "confrontan" ("{}") deve '
            'ter ao menos 3 caracteres!'.format(id_feat, confrontan)
        )


def indexar_vertices(vertice_feats):
    indice = {}
    for feat in vertice_feats:
        for p in pontos_da_geometria(feat.geometry()):
            indice.setdefault(chave_ponto(p.x(), p.y()), feat)
    return indice


def indexar_limites(limite_feats):
    """Indexa cada segmento (A -> B) das linhas de limite."""
    indice = {}
    for feat in limite_feats:
        for linha in linhas_da_geometria(feat.geometry()):
            for a, b in zip(linha[:-1], linha[1:]):
                chave = (chave_ponto(a.x(), a.y()), chave_ponto(b.x(), b.y()))
                indice.setdefault(chave, feat)
    return indice


def normalizar_codigos_excecao(texto):
    """'ALS; bcm' -> ('ALS', 'BCM'). Vazio -> () (correção desativada)."""
    return tuple(
        c.strip().upper()
        for c in re.split(r'[;,\s]+', str(texto or ''))
        if c.strip()
    )


def eh_codigo_excecao(codigo, codigos_excecao):
    """True se o código do vértice pertence ao técnico executante (exceção)."""
    codigo = str(codigo or '').upper()
    return any(c in codigo for c in codigos_excecao)


def casas_decimais(valor):
    """Quantidade de casas decimais de um número (ex.: 101.222 -> 3)."""
    texto = '{:.10f}'.format(round(float(valor), 10)).rstrip('0').rstrip('.')
    return len(texto.split('.')[1]) if '.' in texto else 0


def altitude_do_atributo(feat, dec_prec):
    """Cota Z do campo "Z" da camada vértice, com TODAS as suas casas decimais.

    Nunca usa menos casas que dec_prec (101.2 -> '101,20'; 101.222 -> '101,222').
    Devolve None se o campo Z estiver ausente/inválido.
    """
    z = attr_float(feat, 'Z')

    if z is None or isnan(z):
        return None

    casas = max(dec_prec, casas_decimais(z))
    return ('{:.' + str(casas) + 'f}').format(round(z, 10)).replace('.', ',')


def montar_corpo_parte(numero, anel, idx_vertices, idx_limites,
                       dec_coord, dec_prec, validados, codigos_excecao=()):
    """Comandos da macro para UMA parte (uma aba perimetro_N)."""
    dec_prec_txt = str(dec_prec)

    def fmt(valor):
        return ('{:.' + dec_prec_txt + 'f}').format(valor).replace('.', ',')

    corpo = '\tdoc.activate("perimetro_{}")\n'.format(numero)
    corpo += '\tdoc.setValue("B3", "Parte {}")\n'.format(numero)
    corpo += '\tdoc.setValue("B4", "{:03d}")\n'.format(numero)

    for k1 in range(len(anel) - 1):
        pnt = anel[k1]
        pnt_seg = anel[k1 + 1]

        feat_v = idx_vertices.get(chave_ponto(pnt.x(), pnt.y()))

        if feat_v is None:
            raise QgsProcessingException(
                'Parte {}: ponto de coordenadas ({}, {}) da camada parcela '
                'não possui correspondente na camada vértice!'
                .format(numero, pnt.y(), pnt.x())
            )

        if feat_v.id() not in validados['v']:
            validar_vertice(feat_v)
            validados['v'].add(feat_v.id())

        feat_l = idx_limites.get((
            chave_ponto(pnt.x(), pnt.y()),
            chave_ponto(pnt_seg.x(), pnt_seg.y())
        ))

        if feat_l is None:
            raise QgsProcessingException(
                'Parte {}: limite não encontrado para o segmento de ponto '
                '({}, {}) para ({}, {}). Verifique topologia/sentido da '
                'camada limite.'.format(
                    numero, pnt.y(), pnt.x(), pnt_seg.y(), pnt_seg.x()
                )
            )

        if feat_l.id() not in validados['l']:
            validar_limite(feat_l)
            validados['l'].add(feat_l.id())

        codigo = attr_texto(feat_v, 'vertice')
        longitude = dd2dms(pnt.x(), dec_coord) + 'W'
        latitude = (
            dd2dms(pnt.y(), dec_coord) + 'S' if pnt.y() < 0
            else dd2dms(pnt.y(), dec_coord) + 'N'
        )
        sigma_x = fmt(attr_float(feat_v, 'sigma_x'))
        sigma_y = fmt(attr_float(feat_v, 'sigma_y'))
        sigma_z = fmt(attr_float(feat_v, 'sigma_z'))

        z = z_da_feicao(feat_v)
        altitude_atributo = None

        # Código de OUTRO técnico (não é a exceção): a cota Z da planilha
        # segue o campo Z da camada vértice, preservando suas casas decimais.
        if codigos_excecao and not eh_codigo_excecao(codigo, codigos_excecao):
            altitude_atributo = altitude_do_atributo(feat_v, dec_prec)

        if altitude_atributo is not None:
            altitude = altitude_atributo
            padrao = fmt(z) if not isnan(z) else '0,00'
            if altitude != padrao and feat_v.id() not in validados['z']:
                validados['z'][feat_v.id()] = (codigo, padrao, altitude)
        elif not isnan(z):
            altitude = fmt(z)
        else:
            altitude = '0,00'
            QgsMessageLog.logMessage(
                'Advertência: Ponto de código {} (ID: {}) está com altitude '
                'igual a 0 (zero). Verifique!'.format(codigo, feat_v.id()),
                'GeneratorOds',
                LOG_AVISO
            )

        metodo_pos = attr_texto(feat_v, 'metodo_pos')

        tipo = attr_texto(feat_l, 'tipo')
        confrontan = attr_texto(feat_l, 'confrontan')
        cns = attr_texto(feat_l, 'cns')
        matricula = attr_texto(feat_l, 'matricula')

        k = k1 + 12
        valores = (
            ('A', codigo), ('B', longitude), ('C', sigma_x),
            ('D', latitude), ('E', sigma_y), ('F', altitude),
            ('G', sigma_z), ('H', metodo_pos), ('I', tipo),
            ('J', cns), ('K', matricula), ('L', confrontan)
        )

        for coluna, valor in valores:
            corpo += '\tdoc.setValue("{}{}", "{}")\n'.format(
                coluna, k, escapar_macro(valor)
            )

    return corpo


def criar_abas(data, quantidade_partes):
    """Cria na macro as abas perimetro_2 ... perimetro_N (cópias da anterior)."""
    add_sheets = ''
    act_sheets = ''

    for i in range(1, quantidade_partes):
        add_sheets += (
            '\tdoc.copySheet("perimetro_{}","perimetro_{}","sobre")\n'
            .format(i, i + 1)
        )
        act_sheets += '\tdoc.activate("perimetro_{}")\n'.format(i + 1)
        act_sheets += '#table_{}\n'.format(i + 1)

    data = data.replace('#copy_sheet', add_sheets)
    data = data.replace('#activate_sheet', act_sheets)
    return data


def inserir_tabelas(data, corpos):
    """Substitui '#table_N' pelo corpo de cada parte ('#table_1' não atinge '#table_10')."""
    for numero in sorted(corpos.keys(), reverse=True):
        padrao = re.compile(r'#table_{}(?!\d)'.format(numero))

        if not padrao.search(data):
            raise QgsProcessingException(
                "O arquivo macro.py do plugin não possui o marcador "
                "'#table_{}'.".format(numero)
            )

        corpo = corpos[numero]
        data = padrao.sub(lambda _m, c=corpo: c, data)

    return data


# ---------------- caminhos (multiplataforma) ----------------

def pasta_plugin_shp():
    """Pasta recursos/sigef do plugin (macro.py e modelo .ods)."""
    pasta = os.path.join(PASTA_PLUGIN, 'recursos', 'sigef')

    if not os.path.isfile(os.path.join(pasta, 'macro.py')):
        raise QgsProcessingException(
            "Não foi encontrado o arquivo macro.py do plugin em:\n{}".format(pasta)
        )

    return pasta


def localizar_modelo_ods(pasta_shp):
    exato = os.path.join(pasta_shp, "sigef_planilha_modelo_1.2_rc5.ods")

    if os.path.isfile(exato):
        return exato

    modelos = sorted(glob.glob(os.path.join(pasta_shp, "sigef_planilha_modelo*.ods")))

    if modelos:
        return modelos[-1]

    raise QgsProcessingException(
        "Modelo ODS do SIGEF não encontrado em:\n{}".format(pasta_shp)
    )


def localizar_soffice():
    sistema = platform.system()
    candidatos = []

    if sistema == "Windows":
        candidatos += [
            "C:/Program Files/LibreOffice/program/soffice.exe",
            "C:/Program Files (x86)/LibreOffice/program/soffice.exe",
        ]
    elif sistema == "Darwin":
        candidatos.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    else:
        candidatos.append("/usr/bin/soffice")

    encontrado = shutil.which("soffice")
    if encontrado:
        candidatos.append(encontrado)

    for caminho in candidatos:
        if os.path.isfile(caminho):
            return caminho

    raise QgsProcessingException(
        "LibreOffice (soffice) não encontrado. Caminhos verificados:\n{}"
        .format("\n".join(candidatos))
    )


def pasta_macro_libreoffice():
    sistema = platform.system()
    home = Path.home()

    if sistema == "Windows":
        return home / "AppData/Roaming/LibreOffice/4/user/Scripts/python"
    if sistema == "Darwin":
        return home / "Library/Application Support/LibreOffice/4/user/Scripts/python"
    return home / ".config/libreoffice/4/user/Scripts/python"


def ler_texto_macro(caminho):
    try:
        with open(caminho, 'r', encoding='utf-8') as arq:
            return arq.read(), 'utf-8'
    except UnicodeDecodeError:
        codificacao = locale.getpreferredencoding(False)
        with open(caminho, 'r', encoding=codificacao) as arq:
            return arq.read(), codificacao


# ---------------- geração do ODS de UMA parcela ----------------

def gerar_ods_parcela(parcela, vertices, limites, caminho_saida,
                      dec_coord, dec_prec, feedback, codigos_excecao=()):
    """Preenche o modelo SIGEF (uma aba por parte) via macro do LibreOffice."""
    geom = parcela.geometry()

    # Partes ordenadas por área (maior -> menor): Parte 1 = maior polígono.
    partes = partes_ordenadas_por_area(geom)

    if not partes:
        raise QgsProcessingException(
            "A parcela não possui polígono para gerar a planilha."
        )

    feedback.pushInfo("Partes (abas de perímetro): {}".format(len(partes)))

    idx_vertices = indexar_vertices(vertices)
    idx_limites = indexar_limites(limites)

    # 1) Monta o corpo de cada parte (valida antes de abrir o LibreOffice)
    validados = {'v': set(), 'l': set(), 'z': {}}
    corpos = {}

    for numero, parte in enumerate(partes, start=1):
        # Somente o anel externo contínuo de cada parte.
        anel = preparar_anel(parte[0])

        feedback.pushInfo(
            "  Parte {} (aba {}): {} vértices | área relativa {:.6g}".format(
                numero, numero + 1, len(anel) - 1, area_anel(parte[0])
            )
        )

        corpos[numero] = montar_corpo_parte(
            numero, anel, idx_vertices, idx_limites,
            dec_coord, dec_prec, validados, codigos_excecao
        )

    if validados['z']:
        feedback.pushInfo(
            "Cota Z corrigida pela camada vértice (código de outro técnico): "
            "{} vértice(s)".format(len(validados['z']))
        )
        for codigo, antes, depois in validados['z'].values():
            feedback.pushInfo("  {}: {} -> {}".format(codigo, antes, depois))

    # 2) Prepara a macro
    pasta_shp = pasta_plugin_shp()
    path_ods = localizar_modelo_ods(pasta_shp)
    path_libfile = localizar_soffice()

    path_macro = pasta_macro_libreoffice()
    path_macro.mkdir(parents=True, exist_ok=True)

    arquivo_macro = str(path_macro / 'qgis_macro.py')
    shutil.copy(os.path.join(pasta_shp, 'macro.py'), arquivo_macro)

    try:
        data, codificacao = ler_texto_macro(arquivo_macro)

        data = data.replace(
            "Natureza do serviço",
            NATUREZA_SERVICO.get(attr_int(parcela, 'nat_serv'), '')
        )
        data = data.replace(
            "Tipo de pessoa", TIPO_PESSOA.get(attr_int(parcela, 'pessoa'), '')
        )
        data = data.replace(
            "Situação", SITUACAO.get(attr_int(parcela, 'situacao'), '')
        )
        data = data.replace(
            "Natureza da area", NATUREZA_AREA.get(attr_int(parcela, 'natureza'), '')
        )

        data = data.replace("Nome", escapar_macro(attr_texto(parcela, 'nome')))
        data = data.replace("CPF", escapar_macro(attr_texto(parcela, 'cpf_cnpj')))
        data = data.replace("Denominação", escapar_macro(attr_texto(parcela, 'denominacao')))
        data = data.replace("Codigo do Imovel", escapar_macro(attr_texto(parcela, 'sncr')))
        data = data.replace("Codigo do cartorio", escapar_macro(attr_texto(parcela, 'cod_cartorio')))
        data = data.replace("Matricula", escapar_macro(attr_texto(parcela, 'matricula')))

        municipio = attr_texto(parcela, 'municipio')
        uf = attr_texto(parcela, 'uf')
        municipio_completo = (
            "{}-{}".format(municipio, uf) if municipio and uf
            else municipio or uf
        )
        municipio_a17 = municipio_completo.split(';')[0]

        data = data.replace("Municipio_A17", escapar_macro(municipio_a17))
        data = data.replace("Municipio_B17", escapar_macro(municipio_completo))

        # Abas extras: Parte 2 -> aba 3, Parte 3 -> aba 4, ...
        if len(partes) != 1:
            data = criar_abas(data, len(partes))

        # '/' evita erro 'unicodeescape' no Windows dentro do qgis_macro.py
        data = data.replace('output_path', str(caminho_saida).replace("\\", "/"))

        data = inserir_tabelas(data, corpos)

        with open(arquivo_macro, 'w', encoding=codificacao, errors='replace') as arq:
            arq.write(data)

        # 3) Executa a macro no LibreOffice
        feedback.pushInfo("Executando macro no LibreOffice...")

        subprocess.run([
            path_libfile,
            "--invisible",
            path_ods,
            "vnd.sun.star.script:qgis_macro.py$create_table"
            "?language=Python&location=user"
        ])

    finally:
        try:
            os.remove(arquivo_macro)
        except Exception:
            pass


def gerar_ods_em_planilha_limpa(parcela, vertices, limites, pasta_saida,
                                caminho_final, dec_coord, dec_prec, feedback,
                                codigos_excecao=()):
    """Gera o ODS em pasta temporária exclusiva e só depois grava o arquivo final."""
    if os.path.exists(caminho_final):
        try:
            os.remove(caminho_final)
        except Exception as erro:
            raise QgsProcessingException(
                "Não foi possível remover o ODS de saída anterior:\n{}\n{}"
                .format(caminho_final, erro)
            )

    pasta_temporaria = tempfile.mkdtemp(
        prefix="odspro_ods_lote_", dir=pasta_saida
    )

    caminho_temporario = os.path.join(
        pasta_temporaria, "ODS_LIMPO_{}.ods".format(int(time.time() * 1000000))
    )

    try:
        gerar_ods_parcela(
            parcela, vertices, limites, caminho_temporario,
            dec_coord, dec_prec, feedback, codigos_excecao
        )

        if not os.path.exists(caminho_temporario):
            raise QgsProcessingException(
                "O LibreOffice não criou a planilha temporária:\n{}"
                .format(caminho_temporario)
            )

        # Aguarda o tamanho estabilizar (LibreOffice ainda finalizando o arquivo).
        tamanho_anterior = -1
        for _ in range(20):
            tamanho_atual = os.path.getsize(caminho_temporario)
            if tamanho_atual > 0 and tamanho_atual == tamanho_anterior:
                break
            tamanho_anterior = tamanho_atual
            time.sleep(0.25)

        if os.path.getsize(caminho_temporario) <= 0:
            raise QgsProcessingException(
                "A planilha temporária foi criada, mas está vazia."
            )

        shutil.copy2(caminho_temporario, caminho_final)

        if not os.path.exists(caminho_final) or os.path.getsize(caminho_final) <= 0:
            raise QgsProcessingException(
                "Não foi possível criar o arquivo ODS definitivo:\n{}"
                .format(caminho_final)
            )

    finally:
        shutil.rmtree(pasta_temporaria, ignore_errors=True)


# ============================================================
# ALGORITMO DE PROCESSING
# ============================================================

class OdsProGenerator(QgsProcessingAlgorithm):
    """ODS PRO - ferramenta única, com dois modos de geração.

    - Lote a lote: gera UMA planilha, da parcela selecionada.
    - Em lote: gera uma planilha para cada parcela, em sequência.
    """

    MODO = 'MODO'
    PARCELA = 'PARCELA'
    VERTICE = 'VERTICE'
    LIMITE = 'LIMITE'
    DEC_COORD = 'DEC_COORD'
    DEC_PREC = 'DEC_PREC'
    COD_EXCECAO = 'COD_EXCECAO'
    VER_Z = 'VER_Z'
    SEL_PROXIMO = 'SEL_PROXIMO'
    VAL_COMPLETA = 'VAL_COMPLETA'
    OUTPUT_FOLDER = 'OUTPUT_FOLDER'

    MODO_LOTE_A_LOTE = 0
    MODO_EM_LOTE = 1

    def tr(self, string, string_pt=None):
        if string_pt:
            return QCoreApplication.translate('Processing', string_pt)
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return OdsProGenerator()

    def name(self):
        return 'gerar_ods_sigef'

    def displayName(self):
        return 'ODS PRO'

    def group(self):
        return 'Georreferenciamento'

    def groupId(self):
        return 'georreferenciamento'

    def flags(self):
        # Altera a seleção das camadas do projeto: precisa rodar na thread principal.
        return super().flags() | SEM_THREAD

    def icon(self):
        return QIcon(os.path.join(PASTA_PLUGIN, 'docs', 'images', 'icon_ods.png'))

    def shortHelpString(self):
        # painel de ajuda do formulário: aceita HTML; a imagem é o ícone do ODS PRO
        url_ods = QUrl.fromLocalFile(
            os.path.join(PASTA_PLUGIN, 'docs', 'images', 'icon_ods.png')).toString()
        return (
            f'<p align="center"><img src="{url_ods}" width="220" height="220"></p>'
            '<p>Gera planilhas ODS para o SIGEF, preenchidas automaticamente a '
            'partir das camadas parcela, vértice e limite. Aplicado em projetos '
            'com um ou poucos lotes, ou com centenas e milhares de lotes. '
            'Otimiza a etapa de processamento dos dados dos projetos de '
            'georreferenciamento de imóveis rurais.</p>'
            '<p>Elaborado por Dra. Ranieli dos Anjos de Souza, Dr. Valdir Moura, Maik Rodrigues e Mychelle Novais/p>'
            '<p>Contato para treinamento: restaurear@gmail.com</p>'
            '<p><a href="https://wa.me/5569993512657">Fale conosco</a></p>'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterEnum(
                self.MODO,
                self.tr('Modo de geração'),
                options=[
                    self.tr('Lote a lote (apenas a parcela selecionada)'),
                    self.tr('Em lote (todas as parcelas, a partir da selecionada)'),
                ],
                defaultValue=self.MODO_LOTE_A_LOTE
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.PARCELA,
                self.tr('Camada Parcela'),
                [FONTE_POLIGONO],
                defaultValue=find_layer_by_name("parcela")
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.VERTICE,
                self.tr('Camada Vertice'),
                [FONTE_PONTO],
                defaultValue=find_layer_by_name("vertice")
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.LIMITE,
                self.tr('Camada Limite'),
                [FONTE_LINHA],
                defaultValue=find_layer_by_name("limite")
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.DEC_COORD,
                self.tr('Casas decimais das coordenadas'),
                type=NUMERO_INTEIRO,
                defaultValue=3,
                minValue=3
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.DEC_PREC,
                self.tr('Casas decimais das precisões e altitude'),
                type=NUMERO_INTEIRO,
                defaultValue=2,
                minValue=2
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.COD_EXCECAO,
                self.tr(
                    'Código do técnico executante (exceção). Ex.: ALS. '
                    'Vértices de OUTROS códigos têm a cota Z corrigida pela '
                    'camada vértice, com todas as casas decimais (vazio = desativado)'
                ),
                defaultValue='',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.VER_Z,
                self.tr('Verificar cota Z (nula, zero, baixa precisão |Z| < 10 ou fora de -10 a 3000 m)'),
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SEL_PROXIMO,
                self.tr('Selecionar próxima parcela (apenas no modo lote a lote)'),
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.VAL_COMPLETA,
                self.tr(
                    'Validar projeto completo (apenas no modo lote a lote). '
                    'Se desmarcado, valida apenas a parcela atual selecionada'
                ),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_FOLDER,
                self.tr('Pasta de saída dos ODS')
            )
        )

    def checkParameterValues(self, parameters, context):
        """Exige a pasta de saída (o nome do arquivo segue a denominação)."""
        if not self.parameterAsString(parameters, self.OUTPUT_FOLDER, context):
            return False, self.tr('Informe a "Pasta de saída dos ODS".')

        return super().checkParameterValues(parameters, context)

    # ------------------------------------------------------------------
    # Validações
    # ------------------------------------------------------------------

    def vld_nulos(self, layer):
        """Detecta feições com geometria nula/vazia (seleciona na tabela de atributos)."""
        ids_null = [
            f.id() for f in layer.getFeatures()
            if not f.hasGeometry() or f.geometry().isEmpty()
        ]

        if ids_null:
            layer.selectByIds(ids_null)
            raise QgsProcessingException(
                'Erro: Encontradas {} feição(ões) com geometria nula na camada {} '
                '(feições selecionadas na tabela de atributos). IDs: {}'.format(
                    len(ids_null), layer.name(), ids_null
                )
            )

    def vld_z(self, features, layer):
        """Valida a cota Z (campo "Z") dos vértices informados.

        Reprova o vértice quando o Z é:
        - ausente/nulo ou inválido;
        - igual a zero;
        - de baixa precisão: |Z| < LIMITE_BAIXA_PRECISAO (10);
        - fora de -10 a 3000 m.
        Os vértices reprovados ficam selecionados na tabela de atributos.
        """
        ids_problematicos = []
        msgs = []

        def reprovar(feat, motivo):
            ids_problematicos.append(feat.id())
            msgs.append("  • Vértice '{}' (id {}): {}".format(
                attr_texto(feat, 'vertice'), feat.id(), motivo
            ))

        for feat in features:
            z_value = feat.attribute('Z')

            if z_value is None or z_value == '' or z_value == 'NULL' or _eh_nulo(z_value):
                reprovar(feat, 'Cota Z não preenchida!')
                continue

            try:
                z = float(z_value)
            except (ValueError, TypeError):
                reprovar(feat, 'Cota Z inválida!')
                continue

            if z == 0:
                reprovar(feat, 'Cota Z igual a zero!')
            elif abs(z) < LIMITE_BAIXA_PRECISAO:
                reprovar(feat, 'Cota Z de baixa precisão (|Z| < {}): {}!'.format(
                    LIMITE_BAIXA_PRECISAO, z
                ))
            elif z > 3000 or z < -10:
                reprovar(feat, 'Cota Z ({}) fora dos limites permitidos (-10 a 3000 m)!'.format(z))

        if ids_problematicos:
            layer.selectByIds(ids_problematicos)
            raise QgsProcessingException(
                'Problemas encontrados na Cota Z em {} feição(ões) da camada Vértice '
                '(feições selecionadas na tabela de atributos):\n{}'.format(
                    len(ids_problematicos), '\n'.join(msgs)
                )
            )

    def vld_projeto(self, vertices, limites):
        """Validação do projeto completo (atributos e correspondência limite -> vértice).

        Na validação da parcela atual, os atributos de vértices e limites são
        validados durante o preenchimento de cada parte (somente os usados).
        """
        idx_vertices = indexar_vertices(vertices)

        for feat in vertices:
            validar_vertice(feat)

        for feat in limites:
            validar_limite(feat)

            for linha in linhas_da_geometria(feat.geometry()):
                for pnt in linha:
                    if chave_ponto(pnt.x(), pnt.y()) not in idx_vertices:
                        raise QgsProcessingException(
                            'Erro na feição limite {}: Ponto de coordenadas ({}, {}) '
                            'da camada limite não possui correspondente na camada vértice!'
                            .format(feat.id(), pnt.y(), pnt.x())
                        )

    @staticmethod
    def feicoes_com_geometria(layer):
        return [
            f for f in layer.getFeatures()
            if f.hasGeometry() and not f.geometry().isEmpty()
        ]

    # ------------------------------------------------------------------
    # Processamento
    # ------------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        modo = self.parameterAsEnum(parameters, self.MODO, context)

        parcela_layer = self.parameterAsVectorLayer(parameters, self.PARCELA, context)
        vertice_layer = self.parameterAsVectorLayer(parameters, self.VERTICE, context)
        limite_layer = self.parameterAsVectorLayer(parameters, self.LIMITE, context)

        for param, camada in (
            (self.PARCELA, parcela_layer),
            (self.VERTICE, vertice_layer),
            (self.LIMITE, limite_layer)
        ):
            if camada is None:
                raise QgsProcessingException(self.invalidSourceError(parameters, param))

        validar_banco_geodatabase((
            ('Parcela', parcela_layer),
            ('Vértice', vertice_layer),
            ('Limite', limite_layer),
        ))

        if vertice_layer.featureCount() == 0:
            raise QgsProcessingException('A camada Vértice não pode estar vazia!')
        if limite_layer.featureCount() == 0:
            raise QgsProcessingException('A camada Limite não pode estar vazia!')

        dec_coord = self.parameterAsInt(parameters, self.DEC_COORD, context)
        dec_prec = self.parameterAsInt(parameters, self.DEC_PREC, context)
        ver_z = self.parameterAsBool(parameters, self.VER_Z, context)
        codigos_excecao = normalizar_codigos_excecao(
            self.parameterAsString(parameters, self.COD_EXCECAO, context)
        )

        if codigos_excecao:
            feedback.pushInfo(
                "Código(s) do técnico executante (exceção): {}. Cota Z dos "
                "demais códigos será corrigida pela camada vértice.".format(
                    ", ".join(codigos_excecao)
                )
            )

        pasta_saida = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        if not pasta_saida:
            raise QgsProcessingException('Informe a "Pasta de saída dos ODS".')

        if modo == self.MODO_EM_LOTE:
            return self.processar_em_lote(
                parcela_layer, vertice_layer, limite_layer,
                dec_coord, dec_prec, ver_z, pasta_saida, feedback,
                codigos_excecao
            )

        sel_proximo = self.parameterAsBool(parameters, self.SEL_PROXIMO, context)
        val_completa = self.parameterAsBool(parameters, self.VAL_COMPLETA, context)

        return self.processar_lote_a_lote(
            parcela_layer, vertice_layer, limite_layer,
            dec_coord, dec_prec, ver_z, sel_proximo, val_completa,
            pasta_saida, feedback, codigos_excecao
        )

    # ---------------- modo: lote a lote ----------------

    def processar_lote_a_lote(self, parcela_layer, vertice_layer, limite_layer,
                              dec_coord, dec_prec, ver_z, sel_proximo,
                              val_completa, pasta_saida, feedback,
                              codigos_excecao=()):
        """Gera UMA planilha, da parcela selecionada."""
        selecionadas = parcela_layer.selectedFeatures()

        if len(selecionadas) == 1:
            feature = selecionadas[0]
        elif not selecionadas and parcela_layer.featureCount() == 1:
            feature = next(parcela_layer.getFeatures())
        else:
            raise QgsProcessingException(
                "Selecione exatamente UMA parcela na camada '{}'!".format(
                    parcela_layer.name()
                )
            )

        geom = feature.geometry()

        partes = partes_ordenadas_por_area(geom)
        if not partes:
            raise QgsProcessingException(
                'A parcela não possui polígono para gerar a planilha!'
            )

        try:
            geometria_valida = geom.isGeosValid()
        except Exception:
            geometria_valida = False

        feedback.pushInfo(
            'Partes do polígono: {} | Geometria válida: {}'.format(
                len(partes),
                'sim' if geometria_valida
                else 'NÃO (auto-intersecção/sobreposição ignorada)'
            )
        )

        # Seleção por localização (touches; contorno quando a geometria é inválida)
        if val_completa:
            self.vld_nulos(vertice_layer)
            self.vld_nulos(limite_layer)
            vertices = self.feicoes_com_geometria(vertice_layer)
            limites = self.feicoes_com_geometria(limite_layer)
        else:
            vertices = feicoes_por_localizacao(vertice_layer, geom, parcela_layer.crs())
            limites = feicoes_por_localizacao(limite_layer, geom, parcela_layer.crs())

        feedback.pushInfo(
            'Seleção por localização - vértices: {} | limites: {}'.format(
                len(vertices), len(limites)
            )
        )

        if not vertices:
            raise QgsProcessingException('Nenhum vértice toca a parcela!')
        if not limites:
            raise QgsProcessingException('Nenhum limite toca a parcela!')

        if val_completa:
            self.vld_projeto(vertices, limites)

        if ver_z:
            self.vld_z(vertices, vertice_layer)

        # Nome do arquivo no mesmo padrão do modo em lote (denominacao)
        pasta_saida = os.path.abspath(os.path.expanduser(pasta_saida))
        os.makedirs(pasta_saida, exist_ok=True)

        denominacao = attr_texto(feature, CAMPO_DENOMINACAO)
        output_path = obter_caminho_livre(
            pasta_saida, limpar_nome_arquivo(denominacao)
        )
        feedback.pushInfo("Arquivo de saída: {}".format(output_path))

        # Preenche o modelo do SIGEF: uma aba por parte (maior -> menor área)
        try:
            gerar_ods_em_planilha_limpa(
                feature, vertices, limites, pasta_saida, output_path,
                dec_coord, dec_prec, feedback, codigos_excecao
            )
        except QgsProcessingException:
            raise
        except Exception as e:
            raise QgsProcessingException(
                "Erro ao executar a macro do LibreOffice. Verifique se a versão do "
                "seu LibreOffice ou o seu SO estão atualizados! Detalhes: {}".format(e)
            )

        if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
            raise QgsProcessingException(
                "O LibreOffice não gerou a planilha ODS esperada:\n{}".format(output_path)
            )

        # Seleção automática (vértices, limites e próxima parcela).
        # Executada APÓS a geração para não interferir no processamento.
        self.selecionar_por_parcela(
            parcela_layer, vertice_layer, limite_layer, geom, feedback
        )
        if sel_proximo:
            self.selecionar_proximo(parcela_layer, feature, feedback)

        feedback.pushInfo(
            "\nODS gerada com sucesso!!\n{}\n\n>----FIM DO JOB!!----<\n".format(
                "_" * 18
            )
        )

        return {self.OUTPUT_FOLDER: pasta_saida}

    def selecionar_por_parcela(self, parcela_layer, vertice_layer, limite_layer,
                               parcela_geom, feedback=None):
        """Seleciona vértices e limites da parcela por localização (touches)."""
        # blockSignals suprime sinais Qt (selectionChanged etc.) durante os
        # selectByIds, evitando refresh do canvas com dados ainda em uso
        # (access violation).
        vertice_layer.blockSignals(True)
        limite_layer.blockSignals(True)
        try:
            ids_vertice = selecionar_por_localizacao(
                vertice_layer, parcela_geom, parcela_layer.crs()
            )
            msg_v = '{} vértices selecionados automaticamente por localização.'.format(
                len(ids_vertice)
            )
            QgsMessageLog.logMessage(msg_v, 'GeneratorOds', LOG_INFO)
            if feedback:
                feedback.pushInfo(msg_v)

            ids_limite = selecionar_por_localizacao(
                limite_layer, parcela_geom, parcela_layer.crs()
            )
            msg_l = '{} limites selecionados automaticamente por localização.'.format(
                len(ids_limite)
            )
            QgsMessageLog.logMessage(msg_l, 'GeneratorOds', LOG_INFO)
            if feedback:
                feedback.pushInfo(msg_l)
        finally:
            vertice_layer.blockSignals(False)
            limite_layer.blockSignals(False)
            vertice_layer.triggerRepaint()
            limite_layer.triggerRepaint()

    def selecionar_proximo(self, parcela_layer, feature_atual, feedback=None):
        """Seleciona a próxima parcela da camada (após a parcela atual)."""
        todas_parcelas = list(parcela_layer.getFeatures())
        indice_atual = next(
            (i for i, f in enumerate(todas_parcelas) if f.id() == feature_atual.id()),
            -1
        )

        if indice_atual == -1:
            msg = 'Parcela atual não encontrada para selecionar a próxima.'
            QgsMessageLog.logMessage(msg, 'GeneratorOds', LOG_AVISO)
            if feedback:
                feedback.pushInfo(msg)
            return

        if indice_atual + 1 < len(todas_parcelas):
            proximo_id = todas_parcelas[indice_atual + 1].id()
            parcela_layer.selectByIds([proximo_id])
            msg = "Parcela ID '{}' selecionada.".format(proximo_id)
            QgsMessageLog.logMessage(msg, 'GeneratorOds', LOG_INFO)
        else:
            msg = 'Você está na última parcela.'
            QgsMessageLog.logMessage(msg, 'GeneratorOds', LOG_INFO)

        if feedback:
            feedback.pushInfo(msg)

    # ---------------- modo: em lote ----------------

    def processar_em_lote(self, parcela_layer, vertice_layer, limite_layer,
                          dec_coord, dec_prec, ver_z, pasta_saida, feedback,
                          codigos_excecao=()):
        """Gera uma planilha para cada parcela, a partir da parcela selecionada."""
        pasta_saida = os.path.abspath(os.path.expanduser(pasta_saida))
        os.makedirs(pasta_saida, exist_ok=True)

        if parcela_layer.fields().indexFromName(CAMPO_DENOMINACAO) < 0:
            raise QgsProcessingException(
                "O campo '{}' não existe na camada parcela.".format(CAMPO_DENOMINACAO)
            )

        # ---- parcela inicial ----
        selecionadas = parcela_layer.selectedFeatures()

        if len(selecionadas) != 1:
            raise QgsProcessingException(
                "Selecione exatamente UMA parcela na camada '{}'. "
                "Ela será o primeiro lote do processamento.".format(parcela_layer.name())
            )

        parcela_inicial = selecionadas[0]
        feedback.pushInfo(
            "Parcela inicial: {}".format(attr_texto(parcela_inicial, CAMPO_DENOMINACAO))
        )

        # ---- todas as parcelas, ordenadas por denominacao ----
        parcelas = [
            p for p in parcela_layer.getFeatures()
            if attr_texto(p, CAMPO_DENOMINACAO) != ''
        ]
        # Parcelas com ÁREA ENCRAVADA primeiro; depois as demais.
        # Dentro de cada grupo: ordem natural da denominacao.
        encravadas = {p.id() for p in parcelas if tem_area_encravada(p.geometry())}

        parcelas.sort(key=lambda p: (
            0 if p.id() in encravadas else 1,
            chave_natural(attr_texto(p, CAMPO_DENOMINACAO))
        ))

        if encravadas:
            feedback.pushInfo(
                "Parcelas com área encravada (geradas primeiro): {}".format(
                    ", ".join(
                        attr_texto(p, CAMPO_DENOMINACAO)
                        for p in parcelas if p.id() in encravadas
                    )
                )
            )

        indice_inicio = next(
            (i for i, p in enumerate(parcelas) if p.id() == parcela_inicial.id()),
            None
        )

        if indice_inicio is None:
            raise QgsProcessingException(
                "Não foi possível localizar a parcela selecionada na sequência "
                "de denominação (campo '{}' vazio?).".format(CAMPO_DENOMINACAO)
            )

        parcelas_processar = parcelas[indice_inicio:]
        total = len(parcelas_processar)

        feedback.pushInfo("Parcelas no projeto: {}".format(len(parcelas)))
        feedback.pushInfo("Parcelas a processar: {}".format(total))
        feedback.pushInfo("Pasta de saída: {}".format(pasta_saida))

        gerados = []
        erros = []

        for numero, parcela in enumerate(parcelas_processar, start=1):
            if feedback.isCanceled():
                feedback.pushInfo("Processamento cancelado pelo usuário.")
                break

            denominacao = attr_texto(parcela, CAMPO_DENOMINACAO)
            caminho_saida = obter_caminho_livre(
                pasta_saida, limpar_nome_arquivo(denominacao)
            )

            feedback.pushInfo("\n" + "-" * 60)
            feedback.pushInfo("[{}/{}] {}".format(numero, total, denominacao))

            try:
                geometria = parcela.geometry()

                partes = partes_do_poligono(geometria)

                if not partes:
                    raise QgsProcessingException(
                        "A parcela não possui polígono válido para leitura."
                    )

                try:
                    valida = geometria.isGeosValid()
                except Exception:
                    valida = False

                feedback.pushInfo(
                    "Partes do polígono: {} | Geometria válida: {}".format(
                        len(partes),
                        "sim" if valida
                        else "NÃO (auto-intersecção/sobreposição ignorada)"
                    )
                )

                # Seleção por localização a partir da parcela atual.
                vertices = feicoes_por_localizacao(
                    vertice_layer, geometria, parcela_layer.crs()
                )
                limites = feicoes_por_localizacao(
                    limite_layer, geometria, parcela_layer.crs()
                )

                feedback.pushInfo(
                    "Seleção por localização - vértices: {} | limites: {}".format(
                        len(vertices), len(limites)
                    )
                )

                if not vertices:
                    raise QgsProcessingException("Nenhum vértice toca a parcela.")

                if not limites:
                    raise QgsProcessingException("Nenhum limite toca a parcela.")

                if ver_z:
                    self.vld_z(vertices, vertice_layer)

                gerar_ods_em_planilha_limpa(
                    parcela, vertices, limites, pasta_saida, caminho_saida,
                    dec_coord, dec_prec, feedback, codigos_excecao
                )

                gerados.append(caminho_saida)
                feedback.pushInfo("OK -> {}".format(caminho_saida))

            except Exception as erro:
                mensagem = "{} | {}".format(denominacao, str(erro))
                erros.append(mensagem)
                feedback.reportError("ERRO: " + mensagem, False)
                QgsMessageLog.logMessage(mensagem, 'GeneratorOds', LOG_CRITICO)

            feedback.setProgress(int(100 * numero / total))

        feedback.pushInfo("\n" + "=" * 60)
        feedback.pushInfo("PROCESSAMENTO CONCLUÍDO")
        feedback.pushInfo("ODS gerados: {}".format(len(gerados)))
        feedback.pushInfo("Erros: {}".format(len(erros)))

        if erros:
            feedback.reportError(
                "\nERROS:\n" + "\n".join(" - " + e for e in erros), False
            )

            if not gerados:
                raise QgsProcessingException(
                    "Nenhuma planilha foi gerada. Primeiro erro:\n{}".format(erros[0])
                )

        return {self.OUTPUT_FOLDER: pasta_saida}
