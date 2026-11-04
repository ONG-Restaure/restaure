# -*- coding: utf-8 -*-

"""
ods_sigef
***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************

ODS PRO - gerador de ODS do SIGEF (ferramenta única).

Uso restrito ao banco de dados GeoDataBase (GeoDataBase.gpkg).

Dois modos de geração:

- Lote a lote: gera UMA planilha ODS, da parcela selecionada na camada parcela.
- Em lote: gera uma planilha ODS para cada parcela, em sequência, a partir da
  parcela selecionada. A ordem é pela coluna "denominacao" (ordenação natural:
  Lote 2 antes de Lote 10).

Regras comuns aos dois modos:

- Para cada parcela, vértices e limites são selecionados POR LOCALIZAÇÃO
  (relação "toca"), sem depender do campo "lote".
- Parcelas multiparte geram UMA ODS com uma aba de perímetro por parte,
  ordenadas da MAIOR para a MENOR área: Parte 1 na aba 2, Parte 2 na aba 3...
  Em cada aba, B3 = "Parte N" e B4 = "00N".
- A geometria NÃO é validada/corrigida: auto-intersecção e partes que se
  tocam/sobrepõem são ignoradas.
"""

__author__ = 'Maik Rodrigues e Mychelle Novais'
__date__ = '2026-11-04'
__copyright__ = '(C) 2026 by Maik Rodrigues e Mychelle Novais'

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
    QgsProviderRegistry,
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
# RESTRIÇÃO AO BANCO DE DADOS GeoDataBase
# ============================================================
# O ODS PRO só aceita camadas que venham do GeoPackage "GeoDataBase.gpkg"
# (tabelas: vertice, limite e parcela), com os campos esperados.

GPKG_NOME = 'geodatabase.gpkg'

# Tabela do GeoDataBase -> campos que o ODS PRO lê.
CAMPOS_GEODATABASE = {
    'vertice': ('vertice', 'sigma_x', 'sigma_y', 'sigma_z',
                'metodo_pos', 'tipo_verti', 'z'),
    'limite': ('tipo', 'confrontan', 'cns', 'matricula'),
    'parcela': ('nome', 'nat_serv', 'pessoa', 'cpf_cnpj', 'denominacao',
                'situacao', 'natureza', 'sncr', 'matricula',
                'cod_cartorio', 'municipio', 'uf'),
}


def origem_da_camada(layer):
    """(caminho do arquivo, nome da tabela) de uma camada GeoPackage; senão (None, None)."""
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != 'ogr':
        return None, None
    try:
        partes = QgsProviderRegistry.instance().decodeUri('ogr', layer.source())
    except Exception:
        return None, None
    caminho = partes.get('path') or ''
    tabela = partes.get('layerName') or ''
    if not caminho.lower().endswith('.gpkg'):
        return None, None
    return caminho, tabela


def erro_da_camada(layer, tabela):
    """Texto do problema se a camada NÃO for a tabela `tabela` do GeoDataBase; senão None."""
    caminho, nome = origem_da_camada(layer)

    if caminho is None:
        return ("A camada '{}' não vem do GeoDataBase. Use a tabela '{}' do "
                "GeoPackage GeoDataBase.gpkg.".format(
                    layer.name() if layer else '?', tabela))

    if os.path.basename(caminho).lower() != GPKG_NOME:
        return ("A camada '{}' vem do arquivo '{}'. O ODS PRO só funciona com "
                "o banco GeoDataBase.gpkg.".format(
                    layer.name(), os.path.basename(caminho)))

    if (nome or '').lower() != tabela:
        return ("A camada '{}' é a tabela '{}' do GeoDataBase, mas aqui é "
                "preciso informar a tabela '{}'.".format(
                    layer.name(), nome, tabela))

    campos = {f.name().lower() for f in layer.fields()}
    faltando = [c for c in CAMPOS_GEODATABASE[tabela] if c not in campos]
    if faltando:
        return ("A tabela '{}' do GeoDataBase não tem os campos: {}.".format(
            tabela, ', '.join(faltando)))

    return None


def camada_padrao(tabela):
    """Id da camada do projeto que é a tabela `tabela` do GeoDataBase (ou None)."""
    for layer in QgsProject.instance().mapLayers().values():
        caminho, nome = origem_da_camada(layer)
        if (caminho and os.path.basename(caminho).lower() == GPKG_NOME
                and (nome or '').lower() == tabela):
            return layer.id()
    return None


# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

_SUBSTITUICOES_NOME = {
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
}


def limpar_nome_arquivo(texto):
    """Nome de arquivo somente com letras (A-Z), números e "_".

    PA Amigos do Campo - Lote 009  -> PA_Amigos_do_Campo_Lote_009
    PA_Aliança_Lote_003A           -> PA_Alianca_Lote_003A
    PA_São_Paulo                   -> PA_Sao_Paulo
    Joana D'Arc                    -> Joana_D_Arc
    """
    texto = str(texto).strip()

    # Letras especiais sem decomposição (ß, ø, ł...).
    for origem, destino in _SUBSTITUICOES_NOME.items():
        texto = texto.replace(origem, destino)

    # Remove acentos e cedilha: á->a, ã->a, ç->c, ñ->n ...
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))

    # Qualquer caractere que não seja letra ASCII ou número (espaço, traço,
    # apóstrofo, vírgula, ponto, parênteses...) vira "_".
    texto = re.sub(r"[^A-Za-z0-9]+", "_", texto)

    texto = re.sub(r"_e_", "_", texto)
    texto = re.sub(r"_+", "_", texto)
    return texto.strip("_") or "parcela"


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

        self.tol = 1e-6 if crs.isGeographic() else 1e-2

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
                if geom.touches(self.geom):
                    return True
            except Exception:
                pass
            # "touches" estrito descarta linhas que só diferem por ruído
            # numérico (entram ~1e-9 na parcela): confere pelo contorno.

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
    def _perto(a, b):
        # nós a ~10 cm (ou menos) são o MESMO vértice (ruído de digitalização)
        return ((a.x() - b.x()) ** 2 + (a.y() - b.y()) ** 2) ** 0.5 \
            <= TOLERANCIA_VERTICE

    pts = []

    for p in anel:
        if pts and (
            chave_ponto(p.x(), p.y()) == chave_ponto(pts[-1].x(), pts[-1].y())
            or _perto(p, pts[-1])
        ):
            continue
        pts.append(p)

    # nós finais praticamente iguais ao primeiro (fechamento do anel)
    while len(pts) > 1 and _perto(pts[0], pts[-1]):
        pts.pop()

    if pts:
        pts.append(pts[0])

    if len(pts) < 4:
        raise QgsProcessingException(
            "Anel do polígono com menos de 3 vértices distintos."
        )

    aberto = pts[:-1]
    inicio = max(range(len(aberto)), key=lambda i: aberto[i].y())

    return aberto[inicio:] + aberto[:inicio] + [aberto[inicio]]


LIMITE_BAIXA_PRECISAO = 10


def aviso_analista(mensagem):
    QgsMessageLog.logMessage(
        '[AVISO_ANALISTA] ' + mensagem, 'GeneratorOds', LOG_AVISO
    )


def z_como_esta(feat_v, z):
    """Z com a quantidade de casas decimais que tiver (100 -> 100;
    100.222 -> 100,222), sem arredondar nem acrescentar zeros."""
    bruto = None
    if feat_v.fieldNameIndex('Z') >= 0:
        bruto = feat_v['Z']
    if isinstance(bruto, str) and bruto.strip():
        texto = bruto.strip().replace(',', '.')
        try:
            float(texto)
        except ValueError:
            texto = None
    else:
        texto = None
    if texto is None:
        texto = repr(float(z))
        if 'e' in texto.lower():
            texto = '{:.15f}'.format(z).rstrip('0').rstrip('.')
    if '.' in texto:
        texto = texto.rstrip('0').rstrip('.')
    return texto.replace('.', ',')


def altitude_do_vertice(feat_v, codigo, cod_tecnico, dec_prec, fmt):
    """Altitude (coluna F) do vértice.
      - Z lido do campo "Z" da tabela de atributos da camada vértice;
      - Código do técnico informado (ex.: ALS) e contido no código do
        vértice: SEMPRE 2 casas decimais;
      - Demais códigos (com o código do técnico informado): o Z com a
        quantidade de casas que tiver (100,222 ou 100);
      - Sem código do técnico informado: Z com as casas configuradas;
      - Z ausente/zero/|Z| < 10 gera [AVISO_ANALISTA] (critério do script
        corrigir_precisao_z_ods), mas o valor continua sendo preenchido
        pela regra acima.
    """
    z = attr_float(feat_v, 'Z')
    if z is None and feat_v.fieldNameIndex('Z') >= 0:
        try:
            z = float(str(feat_v['Z']).strip().replace(',', '.'))
        except ValueError:
            z = None
    if z is None:
        z = z_da_feicao(feat_v)
        if isnan(z):
            z = None

    motivo = None
    if z is None:
        motivo = 'Z ausente ou inválido na tabela de atributos'
    elif z == 0:
        motivo = 'Z = 0 na tabela de atributos. Corrija manualmente'
    elif abs(z) < LIMITE_BAIXA_PRECISAO:
        motivo = 'Z de baixa precisão (|Z| < {}): {}. Verifique manualmente'.format(
            LIMITE_BAIXA_PRECISAO, z
        )
    if motivo:
        aviso_analista("Vértice '{}' (ID: {}): {}.".format(
            codigo, feat_v.id(), motivo))

    if z is None:
        return '0,00'

    if cod_tecnico:
        if cod_tecnico in codigo.upper():
            return '{:.2f}'.format(z).replace('.', ',')
        return z_como_esta(feat_v, z)

    return fmt(z)


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


CAMPOS_OBRIGATORIOS_LIMITE = (
    ('confrontan', 'Confrontante'),
    ('tipo', 'Tipo de confrontante'),
)

CAMPOS_OBRIGATORIOS_VERTICE = (
    ('vertice', 'Vértice'),
    ('sigma_x', 'Sigma X'),
    ('sigma_y', 'Sigma Y'),
    ('sigma_z', 'Sigma Z'),
    ('tipo_verti', 'Tipo de vértice'),
    ('metodo_pos', 'Método de posicionamento'),
)


def verificar_preenchimento(vertices, limites):
    """Erro se houver atributos obrigatórios vazios (a ODS NÃO é gerada).

    limite : confrontan, tipo
    vertice: vertice, sigma_x, sigma_y, sigma_z, tipo_verti, metodo_pos
    Lista TODAS as pendências, para o usuário corrigir de uma vez.
    """
    pendencias = []

    for feat in limites:
        vazios = [
            '"{}" ({})'.format(campo, rotulo)
            for campo, rotulo in CAMPOS_OBRIGATORIOS_LIMITE
            if attr_texto(feat, campo) == ''
        ]
        if vazios:
            pendencias.append(
                'camada limite, feição id {}: sem preenchimento em {}'.format(
                    feat.id(), ', '.join(vazios)
                )
            )

    for feat in vertices:
        vazios = [
            '"{}" ({})'.format(campo, rotulo)
            for campo, rotulo in CAMPOS_OBRIGATORIOS_VERTICE
            if attr_texto(feat, campo) == ''
        ]
        if vazios:
            codigo = attr_texto(feat, 'vertice')
            pendencias.append(
                'camada vertice, feição id {}{}: sem preenchimento em {}'.format(
                    feat.id(),
                    ' ({})'.format(codigo) if codigo else '',
                    ', '.join(vazios)
                )
            )

    if pendencias:
        raise QgsProcessingException(
            'Atributos obrigatórios sem preenchimento (ODS não gerada):\n  - '
            + '\n  - '.join(pendencias)
        )


TOLERANCIA_VERTICE = 1e-6  # ~10 cm em graus


class IndiceVertices(dict):
    """Índice exato por coordenada + busca por tolerância (vértice que toca
    a parcela e o limite)."""

    def __init__(self):
        super().__init__()
        self.pontos = []  # (feição, QgsPointXY)

    def buscar(self, pnt, idx_limites=None):
        feat = self.get(chave_ponto(pnt.x(), pnt.y()))
        if feat is not None:
            return feat

        melhor, melhor_d = None, None
        for f, q in self.pontos:
            d = ((q.x() - pnt.x()) ** 2 + (q.y() - pnt.y()) ** 2) ** 0.5
            if d > TOLERANCIA_VERTICE:
                continue
            # o vértice precisa tocar também o limite
            if idx_limites is not None and idx_limites.linhas:
                if min(_dist_ponto_linha(q.x(), q.y(), l)
                       for _, l in idx_limites.linhas) > TOLERANCIA_VERTICE:
                    continue
            if melhor_d is None or d < melhor_d:
                melhor, melhor_d = f, d
        if melhor is not None:
            return melhor

        # último recurso: vértice mais próximo (~1 m), mesmo sem tocar o limite
        for f, q in self.pontos:
            d = ((q.x() - pnt.x()) ** 2 + (q.y() - pnt.y()) ** 2) ** 0.5
            if d <= 1e-5 and (melhor_d is None or d < melhor_d):
                melhor, melhor_d = f, d
        return melhor


def indexar_vertices(vertice_feats):
    indice = IndiceVertices()
    for feat in vertice_feats:
        for p in pontos_da_geometria(feat.geometry()):
            indice.setdefault(chave_ponto(p.x(), p.y()), feat)
            indice.pontos.append((feat, p))
    return indice


TOLERANCIA_LIMITE = 5e-7  # unidades do SRC (~5 cm em graus); absorve ruído numérico


def _dist_ponto_segmento(px, py, a, b):
    ax, ay, bx, by = a.x(), a.y(), b.x(), b.y()
    dx, dy = bx - ax, by - ay
    tam2 = dx * dx + dy * dy
    if tam2 == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / tam2))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _dist_ponto_linha(px, py, linha):
    return min(
        _dist_ponto_segmento(px, py, a, b)
        for a, b in zip(linha[:-1], linha[1:])
    )


class IndiceLimites(dict):
    """Índice exato dos segmentos (A->B e B->A) + busca geométrica de reserva."""

    def __init__(self):
        super().__init__()
        self.linhas = []  # (feição, polilinha)

    def buscar(self, pnt, pnt_seg):
        ka = chave_ponto(pnt.x(), pnt.y())
        kb = chave_ponto(pnt_seg.x(), pnt_seg.y())

        # 1) segmento idêntico, em qualquer sentido
        feat = self.get((ka, kb)) or self.get((kb, ka))
        if feat is not None:
            return feat

        # 2) geométrico: os dois extremos (e o ponto médio) do segmento da
        #    parcela estão sobre a mesma linha de limite (dentro da tolerância).
        mx = (pnt.x() + pnt_seg.x()) / 2.0
        my = (pnt.y() + pnt_seg.y()) / 2.0
        melhor, melhor_d = None, None
        for feat, linha in self.linhas:
            d = max(
                _dist_ponto_linha(pnt.x(), pnt.y(), linha),
                _dist_ponto_linha(pnt_seg.x(), pnt_seg.y(), linha),
                _dist_ponto_linha(mx, my, linha),
            )
            if d <= TOLERANCIA_LIMITE and (melhor_d is None or d < melhor_d):
                melhor, melhor_d = feat, d
        if melhor is not None:
            return melhor

        # 3) último recurso: limite mais próximo do ponto médio (sem tolerância)
        proximo, prox_d = None, None
        for feat, linha in self.linhas:
            d = _dist_ponto_linha(mx, my, linha)
            if prox_d is None or d < prox_d:
                proximo, prox_d = feat, d
        return proximo


def indexar_limites(limite_feats):
    """Indexa cada segmento das linhas de limite (nos dois sentidos)."""
    indice = IndiceLimites()
    for feat in limite_feats:
        for linha in linhas_da_geometria(feat.geometry()):
            indice.linhas.append((feat, linha))
            for a, b in zip(linha[:-1], linha[1:]):
                ka = chave_ponto(a.x(), a.y())
                kb = chave_ponto(b.x(), b.y())
                indice.setdefault((ka, kb), feat)
    return indice


def montar_corpo_parte(numero, anel, idx_vertices, idx_limites,
                       dec_coord, dec_prec, validados, cod_tecnico=''):
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

        feat_v = idx_vertices.buscar(pnt, idx_limites)

        if feat_v is not None and feat_v.id() not in validados['v']:
            try:
                validar_vertice(feat_v)
            except QgsProcessingException as e:
                aviso_analista(str(e))
            validados['v'].add(feat_v.id())

        if feat_v is None:
            aviso_analista(
                'Parte {}: ponto ({}, {}) sem vértice correspondente. '
                'Campos do vértice deixados em branco.'.format(
                    numero, pnt.y(), pnt.x()
                )
            )

        feat_l = idx_limites.buscar(pnt, pnt_seg)

        if feat_l is None:
            aviso_analista(
                'Parte {}: limite não encontrado para o segmento ({}, {}) '
                'para ({}, {}). Campos de limite deixados em branco.'.format(
                    numero, pnt.y(), pnt.x(), pnt_seg.y(), pnt_seg.x()
                )
            )
        elif feat_l.id() not in validados['l']:
            try:
                validar_limite(feat_l)
            except QgsProcessingException as e:
                aviso_analista(str(e))
            validados['l'].add(feat_l.id())

        longitude = dd2dms(pnt.x(), dec_coord) + 'W'
        latitude = (
            dd2dms(pnt.y(), dec_coord) + 'S' if pnt.y() < 0
            else dd2dms(pnt.y(), dec_coord) + 'N'
        )
        if feat_v is None:
            codigo = ''
            sigma_x = sigma_y = sigma_z = fmt(0.0)
            altitude = '0,00'
            metodo_pos = ''
        else:
            codigo = attr_texto(feat_v, 'vertice')
            sigma_x = fmt(attr_float(feat_v, 'sigma_x') or 0.0)
            sigma_y = fmt(attr_float(feat_v, 'sigma_y') or 0.0)
            sigma_z = fmt(attr_float(feat_v, 'sigma_z') or 0.0)
            altitude = altitude_do_vertice(
                feat_v, codigo, cod_tecnico, dec_prec, fmt
            )
            metodo_pos = attr_texto(feat_v, 'metodo_pos')

        if feat_l is None:
            tipo = confrontan = cns = matricula = ''
        else:
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


# ---------------- conexão parcela x vértice x limite (bloqueante) ----------------

def _vertice_estrito(idx_vertices, pnt):
    """Vértice (ponto) exatamente sobre a coordenada, dentro da tolerância.

    Sem "último recurso": se não há vértice sobre o ponto, devolve None.
    """
    feat = idx_vertices.get(chave_ponto(pnt.x(), pnt.y()))
    if feat is not None:
        return feat

    melhor, melhor_d = None, None
    for f, q in idx_vertices.pontos:
        d = ((q.x() - pnt.x()) ** 2 + (q.y() - pnt.y()) ** 2) ** 0.5
        if d <= TOLERANCIA_VERTICE and (melhor_d is None or d < melhor_d):
            melhor, melhor_d = f, d
    return melhor


def _limite_estrito(idx_limites, pnt, pnt_seg):
    """Limite que cobre o segmento da parcela (sem busca pelo mais próximo)."""
    feat = (idx_limites.get((chave_ponto(pnt.x(), pnt.y()),
                             chave_ponto(pnt_seg.x(), pnt_seg.y())))
            or idx_limites.get((chave_ponto(pnt_seg.x(), pnt_seg.y()),
                                chave_ponto(pnt.x(), pnt.y()))))
    if feat is not None:
        return feat

    mx = (pnt.x() + pnt_seg.x()) / 2.0
    my = (pnt.y() + pnt_seg.y()) / 2.0
    melhor, melhor_d = None, None
    for f, linha in idx_limites.linhas:
        d = max(
            _dist_ponto_linha(pnt.x(), pnt.y(), linha),
            _dist_ponto_linha(pnt_seg.x(), pnt_seg.y(), linha),
            _dist_ponto_linha(mx, my, linha),
        )
        if d <= TOLERANCIA_LIMITE and (melhor_d is None or d < melhor_d):
            melhor, melhor_d = f, d
    return melhor


def _no_de_limite_em(idx_limites, pnt):
    """True se algum limite possui um NÓ (extremo ou quebra) sobre o ponto."""
    for _, linha in idx_limites.linhas:
        for q in linha:
            if ((q.x() - pnt.x()) ** 2 + (q.y() - pnt.y()) ** 2) ** 0.5 \
                    <= TOLERANCIA_VERTICE:
                return True
    return False


def _normalizar_nome(texto):
    """Minúsculas, sem acentos e com espaços simples (comparação de nomes)."""
    texto = unicodedata.normalize('NFKD', texto or '')
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    return ' '.join(texto.lower().split())


def verificar_conexao_parcela(parcela, vertices, limites):
    """Erro (a ODS NÃO é gerada) se a parcela estiver desconectada.

    Para o anel externo de CADA parte da parcela, exige:
      1) vértice (ponto) sobre cada vértice do polígono;
      2) limite (linha) cobrindo cada lado do polígono;
      3) limite e parcela conectados no vértice: o vértice da parcela
         coincide com um nó da linha de limite (e com o ponto vértice);
      4) código do vértice NÃO repetido na mesma aba (parte);
      5) confrontante que repete a denominação da parcela deve mencionar
         "parte" (ex.: "Fazenda X - Parte 2").
    Lista TODAS as pendências, para o usuário corrigir de uma vez.
    """
    partes = partes_ordenadas_por_area(parcela.geometry())

    if not partes:
        raise QgsProcessingException(
            "A parcela não possui polígono para gerar a planilha."
        )

    idx_vertices = indexar_vertices(vertices)
    idx_limites = indexar_limites(limites)

    pendencias = []

    denominacao = _normalizar_nome(attr_texto(parcela, CAMPO_DENOMINACAO))
    confrontantes_ja_avisados = set()

    for numero, parte in enumerate(partes, start=1):
        anel = preparar_anel(parte[0])
        codigos_aba = {}  # código normalizado -> primeiro ponto (na aba)

        for k in range(len(anel) - 1):
            pnt = anel[k]
            pnt_seg = anel[k + 1]
            local = 'Parte {}, ponto ({}, {})'.format(numero, pnt.y(), pnt.x())

            feat_v = _vertice_estrito(idx_vertices, pnt)
            if feat_v is None:
                pendencias.append(
                    '{}: falta o VÉRTICE (ponto) na camada vértice'.format(local)
                )
            else:
                codigo = attr_texto(feat_v, 'vertice').strip()
                if codigo:
                    chave = codigo.upper()
                    if chave in codigos_aba:
                        pendencias.append(
                            '{}: código de vértice "{}" REPETIDO na mesma aba '
                            '(já usado no ponto {})'.format(
                                local, codigo, codigos_aba[chave]
                            )
                        )
                    else:
                        codigos_aba[chave] = '({}, {})'.format(pnt.y(), pnt.x())

            feat_l = _limite_estrito(idx_limites, pnt, pnt_seg)
            if feat_l is None:
                pendencias.append(
                    '{}: falta o LIMITE no lado até ({}, {})'.format(
                        local, pnt_seg.y(), pnt_seg.x()
                    )
                )
            elif denominacao and feat_l.id() not in confrontantes_ja_avisados:
                confrontan = _normalizar_nome(attr_texto(feat_l, 'confrontan'))
                if (re.search(r'(?<!\w)' + re.escape(denominacao) + r'(?!\w)',
                              confrontan)
                        and not re.search(r'(?<!\w)parte(?!\w)', confrontan)):
                    confrontantes_ja_avisados.add(feat_l.id())
                    pendencias.append(
                        '{}: confrontante "{}" repete a denominação da parcela '
                        'sem mencionar "parte" (limite id {})'.format(
                            local, attr_texto(feat_l, 'confrontan'), feat_l.id()
                        )
                    )

            if not _no_de_limite_em(idx_limites, pnt):
                pendencias.append(
                    '{}: limite e parcela NÃO estão conectados no vértice '
                    '(a linha de limite não possui nó neste ponto)'.format(local)
                )

    if pendencias:
        raise QgsProcessingException(
            'Parcela com pendência em vértice/limite/confrontante '
            '(ODS não gerada):\n  - ' + '\n  - '.join(pendencias)
        )


# ---------------- geração do ODS de UMA parcela ----------------

def gerar_ods_parcela(parcela, vertices, limites, caminho_saida,
                      dec_coord, dec_prec, feedback, cod_tecnico=''):
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
    validados = {'v': set(), 'l': set()}
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
            dec_coord, dec_prec, validados, cod_tecnico
        )

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
                                cod_tecnico=''):
    """Gera o ODS em pasta temporária exclusiva e só depois grava o arquivo final."""
    # Bloqueia ANTES de qualquer escrita/remoção: polígono sem vértice/limite
    # ou com limite e parcela desconectados no vértice não gera ODS.
    verificar_conexao_parcela(parcela, vertices, limites)

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
            dec_coord, dec_prec, feedback, cod_tecnico
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
    VER_Z = 'VER_Z'
    COD_TECNICO = 'COD_TECNICO'
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
        url_logo = QUrl.fromLocalFile(
            os.path.join(PASTA_PLUGIN, 'docs', 'images', 'icon.png')).toString()    
        return (
            
            '<p>Gera planilhas ODS para o SIGEF, preenchidas automaticamente a '
            'partir das camadas parcela, vértice e limite. Aplicado em projetos '
            'com um ou poucos lotes, ou com centenas e milhares de lotes. '
            'Otimiza a etapa de processamento dos dados dos projetos de '
            'georreferenciamento de imóveis rurais.</p>'
            f'<p align="center"><img src="{url_ods}" width="300" height="300"></p>'
            f'<p align="left"><img src="{url_logo}" width="96" height="96"></p>'

            '<p>Elaborado por Dra. Ranieli dos Anjos de Souza, Dr. Valdir Moura, Maik Rodrigues, Márcio Guerra e Mychelle Novais</p>'
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
                defaultValue=camada_padrao("parcela")
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.VERTICE,
                self.tr('Camada Vertice'),
                [FONTE_PONTO],
                defaultValue=camada_padrao("vertice")
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.LIMITE,
                self.tr('Camada Limite'),
                [FONTE_LINHA],
                defaultValue=camada_padrao("limite")
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
                self.COD_TECNICO,
                self.tr(
                    'Código do técnico executante (exceção). Ex.: ALS (vale se o código do vértice contém este texto). '
                    'Altitude dos vértices com este código preenchida com 2 '
                    'casas decimais; vértices de OUTROS códigos usam a cota Z '
                    'da camada vértice, com todas as casas decimais '
                    '(vazio = desativado)'
                ),
                defaultValue='',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.VER_Z,
                self.tr('Verificar preenchimento de cota Z'),
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
                self.tr('Pasta de saída dos ODS'),
                optional=False,
                createByDefault=False
            )
        )

    def checkParameterValues(self, parameters, context):
        """Exige a pasta de saída e camadas vindas do GeoDataBase."""
        if not self.parameterAsString(parameters, self.OUTPUT_FOLDER, context):
            return False, self.tr('Informe a "Pasta de saída dos ODS".')

        for param, tabela in ((self.PARCELA, 'parcela'),
                              (self.VERTICE, 'vertice'),
                              (self.LIMITE, 'limite')):
            camada = self.parameterAsVectorLayer(parameters, param, context)
            if camada is None:
                continue
            erro = erro_da_camada(camada, tabela)
            if erro:
                return False, self.tr(erro)

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
        """Cota Z nula, zero ou fora de -10 a 3000 m nos vértices informados."""
        ids_problematicos = []
        msgs = []

        for feat in features:
            z_value = feat.attribute('Z')
            if z_value is None or z_value == '' or z_value == 'NULL' or _eh_nulo(z_value):
                ids_problematicos.append(feat.id())
                msgs.append('  • Feição id {}: Cota Z não preenchida ou igual a zero!'.format(feat.id()))
                continue

            try:
                z = float(z_value)
            except (ValueError, TypeError):
                ids_problematicos.append(feat.id())
                msgs.append('  • Feição id {}: Cota Z inválida!'.format(feat.id()))
                continue

            if z == 0:
                ids_problematicos.append(feat.id())
                msgs.append('  • Feição id {}: Cota Z não preenchida ou igual a zero!'.format(feat.id()))
            elif z > 3000 or z < -10:
                ids_problematicos.append(feat.id())
                msgs.append('  • Feição id {}: Cota Z ({}) fora dos limites permitidos (-10 a 3000 m)!'.format(feat.id(), z))

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
                    if idx_vertices.buscar(pnt) is None:
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

        # Uso restrito ao banco de dados GeoDataBase (também fora da interface).
        for camada, tabela in ((parcela_layer, 'parcela'),
                               (vertice_layer, 'vertice'),
                               (limite_layer, 'limite')):
            erro = erro_da_camada(camada, tabela)
            if erro:
                raise QgsProcessingException(erro)

        if vertice_layer.featureCount() == 0:
            raise QgsProcessingException('A camada Vértice não pode estar vazia!')
        if limite_layer.featureCount() == 0:
            raise QgsProcessingException('A camada Limite não pode estar vazia!')

        dec_coord = self.parameterAsInt(parameters, self.DEC_COORD, context)
        dec_prec = self.parameterAsInt(parameters, self.DEC_PREC, context)
        ver_z = self.parameterAsBool(parameters, self.VER_Z, context)
        cod_tecnico = (
            self.parameterAsString(parameters, self.COD_TECNICO, context) or ''
        ).strip().upper()

        pasta_saida = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        if not pasta_saida:
            raise QgsProcessingException('Informe a "Pasta de saída dos ODS".')

        if modo == self.MODO_EM_LOTE:
            return self.processar_em_lote(
                parcela_layer, vertice_layer, limite_layer,
                dec_coord, dec_prec, ver_z, pasta_saida, feedback,
                cod_tecnico
            )

        sel_proximo = self.parameterAsBool(parameters, self.SEL_PROXIMO, context)
        val_completa = self.parameterAsBool(parameters, self.VAL_COMPLETA, context)

        return self.processar_lote_a_lote(
            parcela_layer, vertice_layer, limite_layer,
            dec_coord, dec_prec, ver_z, sel_proximo, val_completa,
            pasta_saida, feedback, cod_tecnico
        )

    # ---------------- modo: lote a lote ----------------

    def processar_lote_a_lote(self, parcela_layer, vertice_layer, limite_layer,
                              dec_coord, dec_prec, ver_z, sel_proximo,
                              val_completa, pasta_saida, feedback,
                              cod_tecnico=''):
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

        # Atributos obrigatórios vazios: não gera a ODS, informa o erro e
        # (se marcado) já seleciona a próxima parcela.
        try:
            if val_completa:
                verificar_preenchimento(
                    feicoes_por_localizacao(vertice_layer, geom, parcela_layer.crs()),
                    feicoes_por_localizacao(limite_layer, geom, parcela_layer.crs())
                )
            else:
                verificar_preenchimento(vertices, limites)
        except QgsProcessingException:
            if sel_proximo:
                try:
                    self.selecionar_proximo(parcela_layer, feature, feedback)
                except Exception:
                    pass
            raise

        if val_completa:
            self.vld_projeto(vertices, limites)

        if ver_z:
            self.vld_z(vertices, vertice_layer)

        pasta_saida = os.path.abspath(os.path.expanduser(pasta_saida))
        os.makedirs(pasta_saida, exist_ok=True)
        # Nome do arquivo = denominação da parcela (substitui se já existir)
        nome_base = limpar_nome_arquivo(
            attr_texto(feature, CAMPO_DENOMINACAO)
        ) or 'parcela'
        output_path = os.path.join(pasta_saida, nome_base + ".ods")

        # Preenche o modelo do SIGEF: uma aba por parte (maior -> menor área)
        try:
            gerar_ods_em_planilha_limpa(
                feature, vertices, limites, pasta_saida, output_path,
                dec_coord, dec_prec, feedback, cod_tecnico
            )
        except QgsProcessingException:
            # Mantém a parcela selecionada: após corrigir, basta executar de novo.
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
        try:
            self.selecionar_por_parcela(
                parcela_layer, vertice_layer, limite_layer, geom, feedback
            )
        except Exception as e:
            QgsMessageLog.logMessage(
                'Falha ao selecionar vértices/limites: {}'.format(e),
                'GeneratorOds', LOG_AVISO
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
        """Seleciona a próxima parcela, na ordem do campo 'denominacao'."""
        def avisar(msg, nivel=LOG_INFO):
            QgsMessageLog.logMessage(msg, 'GeneratorOds', nivel)
            if feedback:
                feedback.pushInfo(msg)

        try:
            todas = list(parcela_layer.getFeatures())
            com_nome = [f for f in todas if attr_texto(f, CAMPO_DENOMINACAO) != '']
            if com_nome and any(f.id() == feature_atual.id() for f in com_nome):
                ordenadas = sorted(
                    com_nome,
                    key=lambda f: chave_natural(attr_texto(f, CAMPO_DENOMINACAO))
                )
            else:
                ordenadas = todas

            indice = next(
                (i for i, f in enumerate(ordenadas) if f.id() == feature_atual.id()),
                -1
            )

            if indice == -1:
                avisar('Parcela atual não encontrada para selecionar a próxima.',
                       LOG_AVISO)
                return

            if indice + 1 >= len(ordenadas):
                avisar('Você está na última parcela.')
                return

            proxima = ordenadas[indice + 1]
            parcela_layer.removeSelection()
            parcela_layer.selectByIds([proxima.id()])
            parcela_layer.triggerRepaint()
            avisar("Próxima parcela selecionada: '{}' (ID {}).".format(
                attr_texto(proxima, CAMPO_DENOMINACAO), proxima.id()))
        except Exception as e:
            avisar('Não foi possível selecionar a próxima parcela: {}'.format(e),
                   LOG_AVISO)

    # ---------------- modo: em lote ----------------

    def processar_em_lote(self, parcela_layer, vertice_layer, limite_layer,
                          dec_coord, dec_prec, ver_z, pasta_saida, feedback,
                          cod_tecnico=''):
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
        parcelas.sort(key=lambda p: chave_natural(attr_texto(p, CAMPO_DENOMINACAO)))

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

                # Atributos obrigatórios vazios: não gera, vai para a próxima.
                verificar_preenchimento(vertices, limites)

                if ver_z:
                    self.vld_z(vertices, vertice_layer)

                gerar_ods_em_planilha_limpa(
                    parcela, vertices, limites, pasta_saida, caminho_saida,
                    dec_coord, dec_prec, feedback, cod_tecnico
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
