# -*- coding: utf-8 -*-
"""
Gerador de APP — algoritmo de Processing do plugin Restaure.

Gera a Área de Preservação Permanente (Lei 12.651/2012, Art. 4º e escadinha
do Art. 61-A) de um ou mais lotes, a partir de um raster de classes já
classificado, e produz tabelas com a área de cada classe por lote.

CÓDIGOS DE CLASSE DO RASTER (ajustáveis nos parâmetros avançados):
    1 = formação florestal        -> floresta ATUAL -> APP sempre CHEIA
    2 = área consolidada          -> zona onde a ESCADINHA se aplica
    3 = corpo hídrico             -> excluído do cálculo de floresta/passivo
    4 = passivo                   -> desmatamento pós-2008 -> APP CHEIA
    5 = floresta em regeneração   -> tratada como área consolidada (ESCADINHA)

Módulos fiscais: baixados automaticamente da tabela oficial do INCRA
("Índices Básicos") na primeira execução, com cache em CSV no perfil do
QGIS, para não baixar de novo a cada execução.

AVISOS:
- Ferramenta de apoio: não substitui análise técnica nem conferência junto
  ao órgão ambiental/CAR.
- Lotes acima de 4 módulos fiscais, em área consolidada (Art. 61-A, § 4º, II:
  faixa definida pelo PRA, entre 20 m e 100 m): o plugin usa a largura do
  Art. 4º de cada rio, limitada a 100 m.
- O teto do Art. 61-B é só reportado numa tabela, não altera a geometria.
"""

import csv
import math
import os
import re
import subprocess
import sys
import urllib.request

import numpy as np
from osgeo import gdal

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsApplication, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterRasterLayer,
    QgsProcessingParameterField, QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink, QgsProcessingUtils,
    QgsProcessingParameterFile,
    QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsProcessingLayerPostProcessorInterface,
    QgsGraduatedSymbolRenderer, QgsLayerTreeLayer, QgsProject, QgsSingleSymbolRenderer, QgsRendererCategory, QgsRendererRange,
    QgsDistanceArea, QgsFields, QgsField, QgsFeature, QgsGeometry, QgsMapLayer,
    QgsVectorLayer, QgsProperty,
)
import processing

from .compat import (
    ARQUIVO_UNICO, FILTRO_CAMPO_NUMERICO, FILTRO_CAMPO_TEXTO, FONTE_LINHA,
    FONTE_PONTO, FONTE_POLIGONO, GEOM_POLIGONO, INSERCAO_RAPIDA, NUMERO_DECIMAL,
    NUMERO_INTEIRO, PARAM_AVANCADO, TIPO_DECIMAL, TIPO_TEXTO, WKB_MULTIPOLIGONO,
)

NOMES_CLASSES = {
    1: "formacao_florestal",
    2: "area_consolidada",
    3: "corpo_hidrico",
    4: "passivo",
    5: "floresta_regeneracao",
}

_LINHA_INCRA_RE = re.compile(
    r"^(?P<codigo>\d{7})\s+"
    r"(?P<nome>[A-ZÀ-Ü0-9'\.\- ]+?)\s+"
    r"(?P<mrg>\d{2,3})\s+"
    r"(?P<zp>\d)\s+"
    r"(?P<modulo_fiscal>\d{1,3})\s+"
    r"(?P<ztm>[A-Z]\d-\d)\s+"
    r"(?P<ie5097>\d)\s+"
    r"(?P<fmp>\d{1,3})\s+"
    r"(?P<imoveis>[\d.]+)\s+"
    r"(?P<area_ha>[\d.,]+)\s+"
    r"(?P<super_territ>[\d.,]+)\s*"
    r"(?P<especiais>.*)$"
)

# O final "/@@display-file/file" é essencial: sem ele o gov.br devolve a página
# HTML de descrição do arquivo, não o PDF em si.
_PASTA_EXT_LIBS = os.path.join(os.path.dirname(__file__), "ext_libs")

URL_PDF_INCRA = (
    "https://www.gov.br/incra/pt-br/acesso-a-informacao/"
    "indices_basicos_2013_por_municipio.pdf/@@display-file/file"
)


def _pasta_cache_incra():
    pasta = os.path.join(QgsApplication.qgisSettingsDirPath(), "restaure_cache")
    os.makedirs(pasta, exist_ok=True)
    return pasta


def _eh_pdf_valido(caminho):
    try:
        with open(caminho, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _baixar_pdf_incra(feedback):
    caminho_pdf = os.path.join(_pasta_cache_incra(), "_indices_basicos_incra.pdf")
    if os.path.exists(caminho_pdf):
        if _eh_pdf_valido(caminho_pdf):
            return caminho_pdf
        # cache de uma tentativa anterior que baixou algo corrompido/incompleto
        feedback.pushInfo("Cache do PDF do INCRA estava corrompido; baixando de novo...")
        os.remove(caminho_pdf)

    feedback.pushInfo(f"Baixando tabela de módulos fiscais do INCRA ({URL_PDF_INCRA}) ...")
    try:
        # alguns sites de governo recusam/truncam pedidos sem User-Agent de
        # navegador, o que gera um PDF corrompido/incompleto no download
        req = urllib.request.Request(
            URL_PDF_INCRA,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=60) as resposta, open(caminho_pdf, "wb") as saida:
            saida.write(resposta.read())
    except Exception as e:
        raise QgsProcessingException(
            f"Não consegui baixar o PDF do INCRA automaticamente ({e}). "
            f"Baixe manualmente de {URL_PDF_INCRA} e salve em {caminho_pdf}, "
            f"depois rode de novo. Ou escolha o arquivo no campo "
            f"'Tabela de módulos fiscais'."
        )

    if not _eh_pdf_valido(caminho_pdf):
        os.remove(caminho_pdf)
        raise QgsProcessingException(
            f"O download do PDF do INCRA terminou, mas o arquivo não é um PDF "
            f"válido (pode ser uma página de erro/bloqueio do site). Baixe "
            f"manualmente de {URL_PDF_INCRA} e salve em {caminho_pdf}, depois "
            f"rode de novo. Ou escolha o arquivo no campo "
            f"'Tabela de módulos fiscais'."
        )

    feedback.pushInfo("Download concluído.")
    return caminho_pdf


def _texto_pdf_com_layout(pdf_path, feedback):
    """Extrai o texto do PDF preservando o alinhamento em colunas (precisa
    disso pra tabela do INCRA fazer sentido). Tenta `pdftotext -layout`
    (Linux/poppler) primeiro; se não existir no sistema (ex.: Windows sem
    poppler no PATH), cai para a biblioteca pypdf em modo "layout"."""
    try:
        return subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            check=True, capture_output=True, text=True
        ).stdout
    except FileNotFoundError:
        feedback.pushInfo("pdftotext não encontrado neste sistema; usando pypdf...")

    try:
        from pypdf import PdfReader
    except ImportError:
        # usa a cópia do pypdf (6.21.3) e do typing_extensions (4.16.0) que vem
        # em ext_libs/. Vai no FIM do sys.path para não passar na frente de
        # versões já instaladas no Python do QGIS.
        if _PASTA_EXT_LIBS not in sys.path:
            sys.path.append(_PASTA_EXT_LIBS)
        try:
            from pypdf import PdfReader
        except Exception as e:
            raise QgsProcessingException(
                f"Não consegui extrair texto do PDF do INCRA: falha ao carregar a "
                f"biblioteca 'pypdf' incluída no plugin ({e}). Escolha um CSV com "
                f"os módulos fiscais no campo 'Tabela de módulos fiscais'."
            )

    leitor = PdfReader(pdf_path)
    paginas = []
    for pagina in leitor.pages:
        try:
            paginas.append(pagina.extract_text(extraction_mode="layout") or "")
        except TypeError:
            # versões mais antigas do pypdf não têm o parâmetro extraction_mode
            paginas.append(pagina.extract_text() or "")
    return "\n".join(paginas)


def _extrair_modulos_fiscais_pdf(pdf_path, feedback):
    txt = _texto_pdf_com_layout(pdf_path, feedback)
    modulos, falhas = {}, 0
    for linha in txt.splitlines():
        linha = linha.strip()
        if not linha or not linha[0].isdigit():
            continue
        m = _LINHA_INCRA_RE.match(linha)
        if not m:
            falhas += 1
            continue
        modulos[m.group("nome").strip().upper()] = float(m.group("modulo_fiscal"))
    feedback.pushInfo(f"[módulos fiscais] {len(modulos)} municípios extraídos "
                       f"({falhas} linhas ignoradas).")
    return modulos


_CACHE_MODULOS = {}


def _achar_coluna(cabecalho, exatos, trechos):
    for i, c in enumerate(cabecalho):
        if c in exatos:
            return i
    for i, c in enumerate(cabecalho):
        if any(t in c for t in trechos):
            return i
    return -1


def _ler_csv_modulos(caminho):
    """Lê um CSV com o nome do município e o módulo fiscal (ha). Aceita vírgula,
    ponto e vírgula ou tab como separador e vírgula decimal. As colunas são
    reconhecidas pelo cabeçalho (ex.: 'municipio' e 'modulo_fiscal_ha')."""
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        amostra = f.read(4096)
        f.seek(0)
        try:
            dialeto = csv.Sniffer().sniff(amostra, delimiters=",;\t")
        except csv.Error:
            dialeto = csv.excel
        leitor = csv.reader(f, dialeto)
        cabecalho = [c.strip().lower() for c in next(leitor, [])]
        i_nome = _achar_coluna(
            cabecalho, ("municipio", "município", "nome_municipio", "nome"), ("munic", "nome"))
        i_mod = _achar_coluna(
            cabecalho, ("modulo_fiscal_ha", "modulo_fiscal", "módulo fiscal", "mod_fisc"),
            ("modulo", "módulo", "mod"))
        if i_nome < 0 or i_mod < 0:
            raise QgsProcessingException(
                "Não reconheci as colunas do CSV de módulos fiscais. O cabeçalho precisa "
                "ter uma coluna com o nome do município (ex.: municipio) e outra com o "
                "módulo fiscal em hectares (ex.: modulo_fiscal_ha).")
        modulos = {}
        for linha in leitor:
            if len(linha) <= max(i_nome, i_mod):
                continue
            nome = linha[i_nome].strip().upper()
            try:
                valor = float(linha[i_mod].strip().replace(",", "."))
            except ValueError:
                continue
            if nome:
                modulos[nome] = valor
    return modulos


def preparar_modulos_fiscais(caminho_tabela, feedback):
    """Carrega a tabela de módulos fiscais UMA vez por execução. Ordem de
    preferência: (1) arquivo escolhido pelo usuário (CSV ou PDF do INCRA), que
    resolve o caso de o link do INCRA mudar; (2) cache em CSV na pasta do perfil
    do QGIS; (3) download automático do PDF do INCRA."""
    _CACHE_MODULOS.clear()

    if caminho_tabela:
        if not os.path.exists(caminho_tabela):
            raise QgsProcessingException(
                f"Não encontrei o arquivo da tabela de módulos fiscais: {caminho_tabela}")
        if caminho_tabela.lower().endswith(".csv"):
            _CACHE_MODULOS.update(_ler_csv_modulos(caminho_tabela))
        else:
            if not _eh_pdf_valido(caminho_tabela):
                raise QgsProcessingException(
                    "O arquivo da tabela de módulos fiscais não é um PDF nem um CSV válido.")
            _CACHE_MODULOS.update(_extrair_modulos_fiscais_pdf(caminho_tabela, feedback))
        if not _CACHE_MODULOS:
            raise QgsProcessingException(
                "O arquivo da tabela de módulos fiscais não trouxe nenhum município.")
        feedback.pushInfo(
            f"Tabela de módulos fiscais lida do arquivo escolhido: "
            f"{len(_CACHE_MODULOS)} municípios.")
        return

    caminho_csv = os.path.join(_pasta_cache_incra(), "modulos_fiscais_incra_cache.csv")
    if os.path.exists(caminho_csv):
        try:
            _CACHE_MODULOS.update(_ler_csv_modulos(caminho_csv))
        except Exception:
            _CACHE_MODULOS.clear()  # cache ilegível: baixa de novo
        if _CACHE_MODULOS:
            return

    pdf_path = _baixar_pdf_incra(feedback)
    modulos = _extrair_modulos_fiscais_pdf(pdf_path, feedback)
    _CACHE_MODULOS.update(modulos)
    with open(caminho_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["municipio", "modulo_fiscal_ha"])
        for nome, mf in sorted(modulos.items()):
            w.writerow([nome, mf])


def obter_modulo_fiscal(nome_municipio, feedback):
    if not _CACHE_MODULOS:
        preparar_modulos_fiscais(None, feedback)
    nome_norm = nome_municipio.strip().upper()
    if nome_norm not in _CACHE_MODULOS:
        raise QgsProcessingException(
            f"Município '{nome_municipio}' não encontrado na tabela de módulos fiscais. "
            f"Confira a grafia (igual à do INCRA). Se a tabela estiver desatualizada, "
            f"apague o arquivo modulos_fiscais_incra_cache.csv da pasta restaure_cache "
            f"ou escolha a tabela no campo 'Tabela de módulos fiscais'.")
    return _CACHE_MODULOS[nome_norm]


def largura_app_escadinha(n_mf):
    """Art. 61-A, §§ 1º a 3º — faixa de recomposição em área consolidada ao
    longo de cursos d'água, para lotes de até 4 módulos fiscais (MF): 5, 8 ou
    15 m. Acima de 4 MF devolve None: nesse caso o plugin usa a largura do
    Art. 4º de cada rio, limitada a 100 m (§ 4º, II: faixa definida pelo PRA,
    de 20 a 100 m)."""
    if n_mf <= 1:
        return 5.0
    elif n_mf <= 2:
        return 8.0
    elif n_mf <= 4:
        return 15.0
    return None


def largura_app_escadinha_lago(n_mf):
    """Art. 61-A, § 6º — faixa reduzida no entorno de LAGOS E LAGOAS NATURAIS
    de verdade (não usada atualmente: o parâmetro LAGOS_LAGOAS está sendo
    tratado como rio em polígono, com a tabela de rio — ver processAlgorithm).
    Fica aqui pronta caso você precise diferenciar lagoa de verdade depois.
    Até 1 MF -> 5 m | 1-2 MF -> 8 m | 2-4 MF -> 15 m | acima de 4 MF -> 30 m."""
    if n_mf <= 1:
        return 5.0
    elif n_mf <= 2:
        return 8.0
    elif n_mf <= 4:
        return 15.0
    return 30.0


def teto_recomposicao_art61b(n_mf, area_lote_ha):
    if n_mf > 10:
        return None
    elif n_mf <= 2:
        return 0.10 * area_lote_ha
    elif n_mf <= 4:
        return 0.20 * area_lote_ha
    return None


# Fragmentos de APP menores que isso (m², só em CRS projetado) são descartados
AREA_MIN_FRAGMENTO_M2 = 10.0

# Mantém os pós-processadores vivos até o QGIS usá-los (se o Python os coletar
# antes, o estilo/contagem não é aplicado).
_POS_PROCESSADORES = []

# ids das camadas de saída já carregadas no projeto nesta execução
_IDS_CAMADAS = {}


def _indice_no(pai, layer_id):
    for i, filho in enumerate(pai.children()):
        if isinstance(filho, QgsLayerTreeLayer) and filho.layerId() == layer_id:
            return i
    return -1


def _ordenar_camadas():
    """Ordem no painel de camadas, de cima para baixo (é a ordem de desenho):
    APP cheia (só contorno) > APP por lote > Classes do lote inteiro. Roda a cada
    camada carregada; a última a ser pós-processada já encontra todas no projeto."""
    ordem = [i for i in (_IDS_CAMADAS.get("cheia"), _IDS_CAMADAS.get("app"),
                         _IDS_CAMADAS.get("lote")) if i]
    if len(ordem) < 2:
        return
    raiz = QgsProject.instance().layerTreeRoot()
    nos = {i: raiz.findLayer(i) for i in ordem}
    if any(n is None for n in nos.values()):
        return
    pai = nos[ordem[0]].parent()
    if pai is None or any(n.parent() != pai for n in nos.values()):
        return
    # de baixo para cima: cada camada precisa ficar acima da que vem depois dela
    for k in range(len(ordem) - 2, -1, -1):
        acima, abaixo = ordem[k], ordem[k + 1]
        i_acima, i_abaixo = _indice_no(pai, acima), _indice_no(pai, abaixo)
        if i_abaixo >= 0 and i_acima > i_abaixo:  # índice 0 é o topo
            copia = nos[acima].clone()
            pai.insertChildNode(i_abaixo, copia)
            pai.removeChildNode(nos[acima])
            nos[acima] = copia

class _PosProcessamento(QgsProcessingLayerPostProcessorInterface):
    """Roda no QGIS depois que a camada de saída foi carregada no projeto:
    liga a contagem de feições e aplica a paleta de cores.
      - tipo "app":  categorias pela coluna 'excede_61b' (vermelho = passa do
                     teto do Art. 61-B)
      - tipo "lote": escala graduada pelo % de passivo no lote inteiro
                     (amarelo claro -> vermelho escuro)"""

    def __init__(self, tipo):
        super().__init__()
        self.tipo = tipo

    @staticmethod
    def _simbolo(cor, contorno="70,70,70,255", largura="0.3"):
        return QgsFillSymbol.createSimple({
            "color": cor, "outline_color": contorno, "outline_width": largura})

    def postProcessLayer(self, layer, context, feedback):
        try:
            _IDS_CAMADAS[self.tipo] = layer.id()
            _ordenar_camadas()
            if not isinstance(layer, QgsVectorLayer):
                return

            campos = layer.fields()
            total = layer.featureCount()

            if self.tipo == "app" and campos.indexFromName("excede_61b") >= 0:
                # a contagem por categoria é feita aqui e escrita no rótulo da legenda
                # (o contador do QGIS ficava em [N/A])
                idx = campos.indexFromName("excede_61b")
                valores = ("sim", "nao", "sem teto", "nao se aplica")
                cont = {v: 0 for v in valores}
                cont["outros"] = 0
                for f in layer.getFeatures():
                    v = f.attributes()[idx]
                    cont[v if v in valores else "outros"] += 1
                layer.setRenderer(QgsCategorizedSymbolRenderer("excede_61b", [
                    QgsRendererCategory(
                        "sim", self._simbolo("215,48,39,160", "165,0,38,255", "0.6"),
                        f"Excede o teto do Art. 61-B ({cont['sim']})"),
                    QgsRendererCategory(
                        "nao", self._simbolo("65,171,93,130", "35,139,69,255"),
                        f"Dentro do teto do Art. 61-B ({cont['nao']})"),
                    QgsRendererCategory(
                        "sem teto", self._simbolo("107,174,214,130", "33,113,181,255"),
                        f"Sem teto, de 4 a 10 MF ({cont['sem teto']})"),
                    QgsRendererCategory(
                        "nao se aplica", self._simbolo("189,189,189,130", "99,99,99,255"),
                        f"Não se aplica, acima de 10 MF ({cont['nao se aplica']})"),
                    QgsRendererCategory(
                        "", self._simbolo("189,189,189,90"),
                        f"Outros, sem classificação ({cont['outros']})"),
                ]))
            elif self.tipo == "cheia":
                # só contorno azul tracejado, para comparar com a APP reduzida
                layer.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple({
                    "color": "0,0,0,0", "outline_color": "0,76,153,255",
                    "outline_width": "0.7", "outline_style": "dash"})))
            elif self.tipo == "lote" and campos.indexFromName("lote_passivo_pct") >= 0:
                faixas = [
                    (0.0, 5.0, "255,255,178,150", "Passivo até 5%"),
                    (5.0, 15.0, "254,204,92,150", "Passivo de 5% a 15%"),
                    (15.0, 30.0, "253,141,60,150", "Passivo de 15% a 30%"),
                    (30.0, 50.0, "240,59,32,150", "Passivo de 30% a 50%"),
                    (50.0, 100.0001, "189,0,38,150", "Passivo acima de 50%"),
                ]
                idx = campos.indexFromName("lote_passivo_pct")
                cont = [0] * len(faixas)
                for f in layer.getFeatures():
                    try:
                        v = float(f.attributes()[idx])
                    except (TypeError, ValueError):
                        continue
                    for k, (ini, fim, _cor, _rot) in enumerate(faixas):
                        if ini <= v <= fim:
                            cont[k] += 1
                            break
                layer.setRenderer(QgsGraduatedSymbolRenderer("lote_passivo_pct", [
                    QgsRendererRange(ini, fim, self._simbolo(cor), f"{rotulo} ({cont[k]})")
                    for k, (ini, fim, cor, rotulo) in enumerate(faixas)]))
            layer.triggerRepaint()

            no = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if no is not None:
                if self.tipo == "cheia":
                    # camada de um símbolo só: a contagem do QGIS funciona
                    no.setCustomProperty("showFeatureCount", True)
                else:
                    # as contagens já estão nos rótulos; o total vai no nome da camada
                    no.setCustomProperty("showFeatureCount", False)
                    layer.setName(f"{layer.name()} [{total}]")
        except Exception as e:  # nunca derruba o algoritmo por causa de estilo
            feedback.pushWarning(f"Não consegui aplicar o estilo/contagem na camada: {e}")


class GerarAPP(QgsProcessingAlgorithm):

    def name(self):
        return "gerar_app"

    def displayName(self):
        return "Gerador de APP"

    def group(self):
        return "Regularização Ambiental"

    def groupId(self):
        return "regularizacao_ambiental"

    def shortHelpString(self):
        # painel de ajuda do formulário: aceita HTML; a logo é o icon.png do plugin
        url_logo = QUrl.fromLocalFile(
            os.path.join(os.path.dirname(__file__), "docs", "images", "icon.png")).toString()
        return (
            f'<p><img src="{url_logo}" width="96" height="96"></p>'
            "<p>Gera área de preservação permanente APP automaticamente, "
            "considerando a escadinha com base na lei 12.651/12.</p>"
            "<p>Elaborado por Dra. Ranieli dos Anjos de Souza e Dr. Valdir Moura</p>"
            "<p>Contato para treinamento: restaurear@gmail.com</p>"
            '<p><a href="https://wa.me/5569993512657">Fale conosco</a></p>'
        )

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "docs", "images", "icon_app.png"))

    def createInstance(self):
        return GerarAPP()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            "HIDROGRAFIA", "Hidrografia (linhas)", [FONTE_LINHA]))
        self.addParameter(QgsProcessingParameterField(
            "CAMPO_LARGURA", "Campo com a largura do curso d'água (m)",
            parentLayerParameterName="HIDROGRAFIA",
            type=FILTRO_CAMPO_NUMERICO, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            "LAGOS_LAGOAS", "Rios / lagos / lagoas em polígono",
            [FONTE_POLIGONO], optional=True))
        self.addParameter(QgsProcessingParameterField(
            "CAMPO_TIPO_POLIGONO", "Campo delimitador de hidrografia",
            parentLayerParameterName="LAGOS_LAGOAS",
            type=FILTRO_CAMPO_TEXTO, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            "NASCENTES", "Nascentes (pontos)", [FONTE_PONTO],
            optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            "LOTES", "Lotes / imóveis (polígonos)", [FONTE_POLIGONO]))
        self.addParameter(QgsProcessingParameterField(
            "CAMPO_MUNICIPIO", "Campo com o nome do município",
            parentLayerParameterName="LOTES", type=FILTRO_CAMPO_TEXTO))
        self.addParameter(QgsProcessingParameterFile(
            "TABELA_MODULOS", "Tabela de módulos fiscais (PDF do INCRA ou CSV)",
            behavior=ARQUIVO_UNICO, optional=True,
            fileFilter="Tabela de módulos fiscais (*.pdf *.csv)"))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "RASTER_CLASSES", "Raster de classes"))
        self.addParameter(QgsProcessingParameterNumber(
            "AREA_MIN_ILHA", "Eliminar ilhas menores que (ha) [0 = não filtra]",
            NUMERO_DECIMAL, 0.0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "SUAVIZACAO", "Suavizar bordas da zona consolidada (iterações) [0 = não suaviza]",
            NUMERO_INTEIRO, 0, minValue=0, maxValue=5))
        self.addParameter(QgsProcessingParameterBoolean(
            "AREA_PELO_VETOR",
            "Calcular áreas pelo vetor",
            defaultValue=False))

        for nome, rotulo, padrao in [
            ("CLASSE_FLORESTAL", "Código da classe: formação florestal", 1),
            ("CLASSE_CONSOLIDADA", "Código da classe: área consolidada", 2),
            ("CLASSE_HIDRICO", "Código da classe: corpo hídrico", 3),
            ("CLASSE_PASSIVO", "Código da classe: passivo", 4),
            ("CLASSE_REGENERACAO", "Código da classe: floresta em regeneração", 5),
        ]:
            p = QgsProcessingParameterNumber(
                nome, rotulo, NUMERO_INTEIRO, padrao, minValue=0)
            p.setFlags(p.flags() | PARAM_AVANCADO)
            self.addParameter(p)

        p = QgsProcessingParameterNumber(
            "RAIO_NASCENTE_CHEIO", "Raio de APP de nascente, largura cheia (m)",
            NUMERO_DECIMAL, 50.0, minValue=0)
        p.setFlags(p.flags() | PARAM_AVANCADO)
        self.addParameter(p)

        p = QgsProcessingParameterNumber(
            "RAIO_NASCENTE_ESCADINHA", "Raio de APP de nascente, escadinha (m)",
            NUMERO_DECIMAL, 15.0, minValue=0)
        p.setFlags(p.flags() | PARAM_AVANCADO)
        self.addParameter(p)

        self.addParameter(QgsProcessingParameterFeatureSink(
            "APP", "APP por lote (classes dentro da APP e checagem do Art. 61-B)",
            FONTE_POLIGONO))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "CLASSES_POR_LOTE", "Classes do lote inteiro",
            FONTE_POLIGONO))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "APP_CHEIA", "APP cheia (Art. 4º, sem escadinha, só para visualização)",
            FONTE_POLIGONO, optional=True, createByDefault=True))

    # -----------------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        hidro = self.parameterAsSource(parameters, "HIDROGRAFIA", context)
        campo_largura = self.parameterAsString(parameters, "CAMPO_LARGURA", context)
        largura_padrao = 5.0  # fixo: qualquer valor <10 cai na mesma faixa (<10m -> 30m)
        nascentes = self.parameterAsSource(parameters, "NASCENTES", context)
        lagos_lagoas = self.parameterAsSource(parameters, "LAGOS_LAGOAS", context)
        campo_tipo_poligono = self.parameterAsString(parameters, "CAMPO_TIPO_POLIGONO", context)
        valor_rio, valor_lago = "rio", "lago"  # valores fixos exigidos na coluna
        lotes = self.parameterAsSource(parameters, "LOTES", context)
        campo_municipio = self.parameterAsString(parameters, "CAMPO_MUNICIPIO", context)
        raster_classes = self.parameterAsRasterLayer(parameters, "RASTER_CLASSES", context)

        classe_florestal = self.parameterAsInt(parameters, "CLASSE_FLORESTAL", context)
        classe_consolidada = self.parameterAsInt(parameters, "CLASSE_CONSOLIDADA", context)
        classe_regeneracao = self.parameterAsInt(parameters, "CLASSE_REGENERACAO", context)
        todas_as_classes = [
            self.parameterAsInt(parameters, "CLASSE_FLORESTAL", context),
            classe_consolidada,
            self.parameterAsInt(parameters, "CLASSE_HIDRICO", context),
            self.parameterAsInt(parameters, "CLASSE_PASSIVO", context),
            classe_regeneracao,
        ]
        raio_nasc_cheio = self.parameterAsDouble(parameters, "RAIO_NASCENTE_CHEIO", context)
        raio_nasc_escada = self.parameterAsDouble(parameters, "RAIO_NASCENTE_ESCADINHA", context)

        if hidro is None:
            raise QgsProcessingException("Hidrografia inválida.")
        if lotes is None:
            raise QgsProcessingException("Camada de lotes inválida.")
        if raster_classes is None:
            raise QgsProcessingException("Raster de classes inválido.")

        caminho_raster = raster_classes.source()
        area_min_ilha_ha = self.parameterAsDouble(parameters, "AREA_MIN_ILHA", context)
        iter_suavizacao = self.parameterAsInt(parameters, "SUAVIZACAO", context)
        area_pelo_vetor = self.parameterAsBool(parameters, "AREA_PELO_VETOR", context)
        if area_min_ilha_ha > 0:
            # o raster filtrado passa a valer para TUDO (zona consolidada e
            # contagem de classes), para os números e o mapa ficarem coerentes
            caminho_raster = self._filtrar_ilhas(
                caminho_raster, area_min_ilha_ha, classe_florestal, feedback)

        # IMPORTANTE: para alimentar sub-algoritmos (processing.run) usamos
        # SEMPRE o valor original do parâmetro (parameters["X"]), nunca o
        # QgsProcessingFeatureSource já resolvido por parameterAsSource — esse
        # objeto é só para ESTE algoritmo ler/iterar, e não é reaproveitável
        # como INPUT de outro algoritmo (o Processing não consegue
        # "re-resolver" a fonte a partir dele, e falha com "valor inválido").
        hidrografia_param = parameters["HIDROGRAFIA"]
        lotes_param = parameters["LOTES"]
        nascentes_param = parameters.get("NASCENTES")
        lagos_lagoas_param = parameters.get("LAGOS_LAGOAS")

        # ---- Grava um id de lote explícito, que sobrevive a merge/interseção ----
        lotes_com_fid = processing.run("native:fieldcalculator", {
            "INPUT": lotes_param, "FIELD_NAME": "fid_lote", "FIELD_TYPE": 1,
            "FIELD_LENGTH": 10, "FIELD_PRECISION": 0, "FORMULA": "$id",
            "OUTPUT": "memory:lotes_com_fid"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        # ---- APP cheia (Art. 4º), uma vez só para toda a área -----------------
        feedback.pushInfo("Gerando APP cheia (Art. 4º) ao longo da hidrografia...")
        if campo_largura:
            expr_largura = f'coalesce("{campo_largura}", {largura_padrao})'
        else:
            expr_largura = str(largura_padrao)
        expr_largura_app4 = f"""
            CASE
                WHEN ({expr_largura}) < 10  THEN 30
                WHEN ({expr_largura}) < 50  THEN 50
                WHEN ({expr_largura}) < 200 THEN 100
                WHEN ({expr_largura}) < 600 THEN 200
                ELSE 500
            END
        """
        hidro_com_largura = processing.run("native:fieldcalculator", {
            "INPUT": hidrografia_param, "FIELD_NAME": "largura_app4", "FIELD_TYPE": 0,
            "FIELD_LENGTH": 10, "FIELD_PRECISION": 2, "FORMULA": expr_largura_app4,
            "OUTPUT": "memory:hidro_largura_app4"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        buffer_app4 = processing.run("native:buffer", {
            "INPUT": hidro_com_largura, "DISTANCE": QgsProperty.fromExpression('"largura_app4"'),
            # capa REDONDA: com capa plana, cada trecho de rio termina em corte reto
            # e, onde dois trechos se encontram numa curva, sobra uma cunha sem APP
            "SEGMENTS": 16, "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
            "DISSOLVE": True, "OUTPUT": "memory:buffer_app4"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        # Mantidos SEPARADOS (não unidos ainda): rio, nascente e lago têm cada
        # um sua própria tabela de redução na escadinha (Art. 61-A, §§3º/4º
        # para rio; §6º para lago — valores diferentes), então cada um precisa
        # da sua própria combinação "cheia fora da zona + escadinha dentro
        # dela" por lote, mais abaixo.
        buffer_nasc_cheio = None
        if nascentes is not None:
            buffer_nasc_cheio = processing.run("native:buffer", {
                "INPUT": nascentes_param, "DISTANCE": raio_nasc_cheio, "SEGMENTS": 16,
                "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
                "DISSOLVE": True, "OUTPUT": "memory:buffer_nasc_cheio"
            }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        # Se houver campo de tipo, separa o polígono em dois subconjuntos:
        # RIO em polígono (largura média estimada -> tabela do Art. 4º, I) e
        # LAGO/LAGOA de verdade (área em ha -> tabela do Art. 4º, III). Sem
        # campo de tipo, tudo é tratado como rio (compatibilidade simples).
        rios_poligono, lagos_poligono = None, None
        if lagos_lagoas is not None:
            if campo_tipo_poligono:
                rios_poligono = processing.run("native:extractbyexpression", {
                    "INPUT": lagos_lagoas_param,
                    "EXPRESSION": f'"{campo_tipo_poligono}" = \'{valor_rio}\'',
                    "OUTPUT": "memory:rios_poligono"
                }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
                lagos_poligono = processing.run("native:extractbyexpression", {
                    "INPUT": lagos_lagoas_param,
                    "EXPRESSION": f'"{campo_tipo_poligono}" = \'{valor_lago}\'',
                    "OUTPUT": "memory:lagos_poligono"
                }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
                if self._camada(rios_poligono, context).featureCount() == 0:
                    rios_poligono = None
                if self._camada(lagos_poligono, context).featureCount() == 0:
                    lagos_poligono = None
            else:
                rios_poligono = lagos_lagoas_param

        buffer_rios_poligono_cheio = None
        if rios_poligono is not None:
            # RIO em polígono: largura média estimada por 2*área/perímetro
            # (aproximação padrão pra polígono alongado), aplicada na MESMA
            # tabela do Art. 4º, I usada na hidrografia em linha.
            expr_largura_media = "2 * area($geometry) / perimeter($geometry)"
            expr_largura_app4_rio_poligono = f"""
                CASE
                    WHEN ({expr_largura_media}) < 10  THEN 30
                    WHEN ({expr_largura_media}) < 50  THEN 50
                    WHEN ({expr_largura_media}) < 200 THEN 100
                    WHEN ({expr_largura_media}) < 600 THEN 200
                    ELSE 500
                END
            """
            rios_poligono_com_largura = processing.run("native:fieldcalculator", {
                "INPUT": rios_poligono, "FIELD_NAME": "largura_app_rio_pol", "FIELD_TYPE": 0,
                "FIELD_LENGTH": 10, "FIELD_PRECISION": 2,
                "FORMULA": expr_largura_app4_rio_poligono,
                "OUTPUT": "memory:rios_poligono_com_largura"
            }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
            buffer_rios_poligono_cheio = processing.run("native:buffer", {
                "INPUT": rios_poligono_com_largura,
                "DISTANCE": QgsProperty.fromExpression('"largura_app_rio_pol"'),
                "SEGMENTS": 16, "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
                "DISSOLVE": True, "OUTPUT": "memory:buffer_rios_poligono_cheio"
            }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        buffer_lagos_cheio = None
        if lagos_poligono is not None:
            # LAGO/LAGOA de verdade: Art. 4º, III — 50 m até 20 ha de
            # superfície, 100 m acima disso (área rural).
            lagos_com_largura = processing.run("native:fieldcalculator", {
                "INPUT": lagos_poligono, "FIELD_NAME": "largura_app_lago", "FIELD_TYPE": 0,
                "FIELD_LENGTH": 10, "FIELD_PRECISION": 2,
                "FORMULA": "CASE WHEN area($geometry) / 10000 <= 20 THEN 50 ELSE 100 END",
                "OUTPUT": "memory:lagos_com_largura"
            }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
            buffer_lagos_cheio = processing.run("native:buffer", {
                "INPUT": lagos_com_largura,
                "DISTANCE": QgsProperty.fromExpression('"largura_app_lago"'),
                "SEGMENTS": 16, "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
                "DISSOLVE": True, "OUTPUT": "memory:buffer_lagos_cheio"
            }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        if feedback.isCanceled():
            return {}

        # ---- Zona consolidada (classe 2), direto do raster --------------------
        feedback.pushInfo("Identificando zona consolidada (classes 2 e 5) no raster...")
        zona_escadinha = self._construir_zona_escadinha(
            caminho_raster, classe_consolidada, classe_regeneracao, context, feedback,
            iter_suavizacao)
        area_pixel_ha = self._tamanho_pixel_ha(caminho_raster)

        tabela_modulos = self.parameterAsFile(parameters, "TABELA_MODULOS", context)
        preparar_modulos_fiscais(tabela_modulos, feedback)

        # ---- Processa cada lote -------------------------------------------------
        lotes_layer = self._camada(lotes_com_fid, context)
        # identificador do lote: se a camada tiver uma coluna "lote" (em qualquer
        # caixa de letra), ela é mantida nas saídas; senão usa o id interno
        campo_id = "fid_lote"
        for fld in lotes_layer.fields():
            if fld.name().lower() == "lote":
                campo_id = fld.name()
                break
        campos_lote = QgsFields(lotes_layer.fields())
        feicoes_app = []
        extras_por_fid = {}

        # APP CHEIA (Art. 4º, sem a escadinha): é igual para todos os lotes, então
        # a união das faixas cheias é montada uma vez só e recortada por lote
        cheias = [b for b in (buffer_app4, buffer_nasc_cheio, buffer_rios_poligono_cheio,
                              buffer_lagos_cheio) if b is not None]
        geoms_cheias = []
        for ref in cheias:
            for f_c in self._camada(ref, context).getFeatures():
                g_c = f_c.geometry()
                if g_c is not None and not g_c.isEmpty():
                    geoms_cheias.append(g_c)
        app_cheia_unida = QgsGeometry.unaryUnion(geoms_cheias) if geoms_cheias else None
        if app_cheia_unida is not None and not app_cheia_unida.isGeosValid():
            app_cheia_unida = app_cheia_unida.makeValid()
        geografico = lotes.sourceCrs().isGeographic()
        medidor_area = self._medidor_area(lotes.sourceCrs(), context)
        idx_id_lote = lotes_layer.fields().indexFromName(campo_id)
        campos_cheia = QgsFields()
        if idx_id_lote >= 0:
            campos_cheia.append(lotes_layer.fields().field(idx_id_lote))
        campos_cheia.append(QgsField("app_cheia_ha", TIPO_DECIMAL))
        campos_cheia.append(QgsField("reducao_escadinha_ha", TIPO_DECIMAL))
        feicoes_cheia = []
        for feat in lotes_layer.getFeatures():
            if feedback.isCanceled():
                return {}
            fid = feat["fid_lote"]
            area_lote_ha = feat.geometry().area() / 10000.0
            nome_municipio = feat[campo_municipio]
            modulo_fiscal_ha = obter_modulo_fiscal(nome_municipio, feedback)
            n_mf = area_lote_ha / modulo_fiscal_ha
            pequena_propriedade = n_mf <= 4.0
            # o Art. 61-A não tem limite de módulos fiscais: toda área consolidada
            # recebe a faixa reduzida (acima de 4 MF vale o § 4º, II)
            aplica_escadinha = True

            feedback.pushInfo(
                f"Lote fid={fid} | município={nome_municipio} | área={area_lote_ha:.2f} ha "
                f"| MF={n_mf:.3f} | pequena propriedade={pequena_propriedade}"
            )

            componentes_lote = []  # resultado (cheio ou reduzido) de cada feição

            # Faixa dos rios em área consolidada:
            #  - até 4 MF: 5, 8 ou 15 m (Art. 61-A, §§ 1º a 3º), fixa
            #  - acima de 4 MF: largura do Art. 4º de cada rio, limitada a 100 m
            if n_mf <= 4:
                largura_escada_rio = largura_app_escadinha(n_mf)
                dist_rio_linha = dist_rio_pol = largura_escada_rio
                entrada_rio_linha = hidrografia_param
                entrada_rio_pol = rios_poligono
            else:
                dist_rio_linha = QgsProperty.fromExpression('least("largura_app4", 100)')
                entrada_rio_linha = hidro_com_largura
                dist_rio_pol = QgsProperty.fromExpression('least("largura_app_rio_pol", 100)')
                entrada_rio_pol = (rios_poligono_com_largura
                                   if buffer_rios_poligono_cheio is not None else None)

            # --- rio: Art. 61-A, §§ 1º a 4º ---------------------------------------
            if aplica_escadinha:
                buffer_escada_rio = processing.run("native:buffer", {
                    "INPUT": entrada_rio_linha, "DISTANCE": dist_rio_linha, "SEGMENTS": 16,
                    "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
                    "DISSOLVE": True, "OUTPUT": "memory:buffer_escada_rio_lote"
                }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
                componentes_lote.append(
                    self._combinar_cheio_escadinha(
                        buffer_app4, buffer_escada_rio, zona_escadinha, context, feedback))
            else:
                componentes_lote.append(buffer_app4)

            # --- nascente: sem tabela própria de redução no Art. 61-A, mas o
            # raio escadinha (15 m, parâmetro) é usado dentro da zona --------------
            if buffer_nasc_cheio is not None:
                if aplica_escadinha:
                    buffer_nasc_escada = processing.run("native:buffer", {
                        "INPUT": nascentes_param, "DISTANCE": raio_nasc_escada, "SEGMENTS": 16,
                        "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
                        "DISSOLVE": True, "OUTPUT": "memory:buffer_nasc_escada_lote"
                    }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
                    componentes_lote.append(
                        self._combinar_cheio_escadinha(
                            buffer_nasc_cheio, buffer_nasc_escada, zona_escadinha,
                            context, feedback))
                else:
                    componentes_lote.append(buffer_nasc_cheio)

            # --- rio em polígono: tratado como rio (mesma tabela do Art. 61-A
            # §§3º/4º), só muda como a largura foi estimada (largura média) ---
            if buffer_rios_poligono_cheio is not None:
                if aplica_escadinha:
                    buffer_escada_rio_pol = processing.run("native:buffer", {
                        "INPUT": entrada_rio_pol, "DISTANCE": dist_rio_pol, "SEGMENTS": 16,
                        "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
                        "DISSOLVE": True, "OUTPUT": "memory:buffer_escada_rio_pol_lote"
                    }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
                    componentes_lote.append(
                        self._combinar_cheio_escadinha(
                            buffer_rios_poligono_cheio, buffer_escada_rio_pol, zona_escadinha,
                            context, feedback))
                else:
                    componentes_lote.append(buffer_rios_poligono_cheio)

            # --- lago/lagoa de verdade: Art. 61-A §6º (tabela própria) -----------
            if buffer_lagos_cheio is not None:
                if aplica_escadinha:
                    largura_escada_lago = largura_app_escadinha_lago(n_mf)
                    buffer_escada_lago = processing.run("native:buffer", {
                        "INPUT": lagos_poligono, "DISTANCE": largura_escada_lago, "SEGMENTS": 16,
                        "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2,
                        "DISSOLVE": True, "OUTPUT": "memory:buffer_escada_lago_lote"
                    }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
                    componentes_lote.append(
                        self._combinar_cheio_escadinha(
                            buffer_lagos_cheio, buffer_escada_lago, zona_escadinha,
                            context, feedback))
                else:
                    componentes_lote.append(buffer_lagos_cheio)

            app_lote = componentes_lote[0]
            for comp in componentes_lote[1:]:
                app_lote = processing.run("native:union", {
                    "INPUT": app_lote, "OVERLAY": comp,
                    "OUTPUT": "memory:app_uniao_lote"
                }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

            # Junta todas as feições da APP deste lote numa geometria só e recorta
            # pelo contorno do lote, em Python: assim sai UMA feição por lote,
            # com exatamente os atributos originais do lote (sem conflito de
            # nomes de campo entre hidrografia e lotes).
            geoms_app = []
            for f_app in self._camada(app_lote, context).getFeatures():
                g = f_app.geometry()
                if g is not None and not g.isEmpty():
                    geoms_app.append(g)

            app_geom = None
            if geoms_app:
                uniao = QgsGeometry.unaryUnion(geoms_app)
                if not uniao.isGeosValid():
                    uniao = uniao.makeValid()
                lote_geom = feat.geometry()
                if not lote_geom.isGeosValid():
                    lote_geom = lote_geom.makeValid()
                inter = uniao.intersection(lote_geom)
                if inter is not None and not inter.isEmpty():
                    partes = [p for p in inter.asGeometryCollection()
                              if p.type() == GEOM_POLIGONO]
                    if not lotes.sourceCrs().isGeographic():
                        # lascas de APP menores que 10 m² (0,001 ha) vêm de
                        # encontros de limites e não são APP de verdade
                        partes = [p for p in partes if p.area() >= AREA_MIN_FRAGMENTO_M2]
                    if partes:
                        app_geom = QgsGeometry.collectGeometry(partes)

            if app_geom is not None and not app_geom.isEmpty():
                nova = QgsFeature(campos_lote)
                nova.setAttributes(feat.attributes())
                nova.setGeometry(app_geom)
                feicoes_app.append(nova)
            else:
                feedback.pushWarning(f"Lote fid={fid}: nenhuma APP dentro do lote.")

            # APP cheia deste lote e quanto a escadinha reduziu
            cheia_geom = self._recortar_poligonos(
                app_cheia_unida, feat.geometry(), geografico)
            if cheia_geom is not None:
                area_cheia = self._area_ha(cheia_geom, medidor_area)
                area_final = self._area_ha(app_geom, medidor_area)
                nf_cheia = QgsFeature(campos_cheia)
                nf_cheia.setGeometry(cheia_geom)
                nf_cheia.setAttributes(
                    ([feat.attributes()[idx_id_lote]] if idx_id_lote >= 0 else [])
                    + [area_cheia, max(area_cheia - area_final, 0.0)])
                feicoes_cheia.append(nf_cheia)

            extras_por_fid[fid] = {
                "modulos_fiscais": n_mf,
                "pequena_propriedade": pequena_propriedade,
                "area_lote_ha": area_lote_ha,
                "teto_61b_ha": teto_recomposicao_art61b(n_mf, area_lote_ha),
            }

        # ---- Camada de APP por lote (uma feição por lote) ------------------------
        crs = lotes.sourceCrs()
        # Criada pelo mecanismo do Processing (e não como objeto Python solto)
        # para que a camada continue sob posse do contexto, como as demais.
        sink_tmp, id_app_tmp = QgsProcessingUtils.createFeatureSink(
            "memory:app_por_lote", context, campos_lote, WKB_MULTIPOLIGONO, crs)
        for nova in feicoes_app:
            sink_tmp.addFeature(nova, INSERCAO_RAPIDA)
        del sink_tmp  # garante a gravação antes de ler a camada
        app_por_lote = self._camada(id_app_tmp, context)

        # ---- Classes por lote (área total) e por lote (dentro da APP) -----------
        feedback.pushInfo("Calculando área de cada classe por lote...")
        if area_pelo_vetor:
            feedback.pushInfo("Modo: área geométrica (raster vetorizado x polígonos).")
            poligonos_classes = self._poligonizar_classes(
                caminho_raster, lotes_com_fid, context, feedback)

            def tabela(alvo, prefixo):
                return self._tabela_classes_vetor(
                    alvo, poligonos_classes, prefixo, todas_as_classes,
                    extras_por_fid, campo_id, classe_consolidada, context, feedback)
        else:
            feedback.pushInfo("Modo: contagem de pixels (histograma zonal).")

            def tabela(alvo, prefixo):
                hist = self._histograma(alvo, caminho_raster, prefixo, context, feedback)
                return self._montar_tabela_classes(
                    hist, prefixo, todas_as_classes, area_pixel_ha, extras_por_fid,
                    campo_id, classe_consolidada)

        # ---- Grava nos sinks de saída --------------------------------------------
        # 1) APP por lote: só a parte do lote que é APP, com as classes medidas
        #    DENTRO da APP e a checagem do teto do Art. 61-B
        if feicoes_app:
            campos_t_app, feicoes_t_app = tabela(app_por_lote, "app")
        else:
            campos_t_app, feicoes_t_app = QgsFields(), []
        sink_app, id_app = self.parameterAsSink(
            parameters, "APP", context, campos_t_app, WKB_MULTIPOLIGONO, crs)
        for f in feicoes_t_app:
            sink_app.addFeature(f, INSERCAO_RAPIDA)

        # 2) Classes do lote INTEIRO (contorno completo de cada lote)
        campos_t_lote, feicoes_t_lote = tabela(lotes_com_fid, "lote")
        sink_lote, id_lote = self.parameterAsSink(
            parameters, "CLASSES_POR_LOTE", context, campos_t_lote,
            WKB_MULTIPOLIGONO, crs)
        for f in feicoes_t_lote:
            sink_lote.addFeature(f, INSERCAO_RAPIDA)

        # 3) APP cheia (opcional): só para visualização
        id_cheia = None
        if feicoes_cheia:
            try:
                sink_cheia, id_cheia = self.parameterAsSink(
                    parameters, "APP_CHEIA", context, campos_cheia,
                    WKB_MULTIPOLIGONO, crs)
            except Exception as e:
                sink_cheia, id_cheia = None, None
                feedback.pushWarning(f"Não consegui criar a camada de APP cheia: {e}")
            if sink_cheia is not None:
                for f in feicoes_cheia:
                    sink_cheia.addFeature(f, INSERCAO_RAPIDA)
            else:
                id_cheia = None

        # Ao carregar no projeto: contagem de feições ligada e paleta de cores
        # (APP por status do Art. 61-B; lote inteiro por % de passivo)
        del _POS_PROCESSADORES[:-6]
        _IDS_CAMADAS.clear()
        for dest_id, tipo in ((id_app, "app"), (id_lote, "lote"), (id_cheia, "cheia")):
            if dest_id and context.willLoadLayerOnCompletion(dest_id):
                pos = _PosProcessamento(tipo)
                _POS_PROCESSADORES.append(pos)  # mantém vivo até o pós-processamento
                context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(pos)

        resultado = {"APP": id_app, "CLASSES_POR_LOTE": id_lote}
        if id_cheia:
            resultado["APP_CHEIA"] = id_cheia
        feedback.pushInfo(f"Lotes processados: {len(extras_por_fid)}")
        return resultado

    # -----------------------------------------------------------------------
    # Auxiliares de raster

    def _combinar_cheio_escadinha(self, buffer_cheio, buffer_escadinha, zona_escadinha,
                                   context, feedback):
        """Largura cheia fora da zona consolidada + largura reduzida dentro
        dela, para UMA feição (rio, nascente ou lago) por vez."""
        fora_zona = processing.run("native:difference", {
            "INPUT": buffer_cheio, "OVERLAY": zona_escadinha,
            "OUTPUT": "memory:fora_zona"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
        dentro_zona = processing.run("native:intersection", {
            "INPUT": buffer_escadinha, "OVERLAY": zona_escadinha,
            "OUTPUT": "memory:dentro_zona"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
        return processing.run("native:union", {
            "INPUT": fora_zona, "OVERLAY": dentro_zona,
            "OUTPUT": "memory:combinado"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

    def _filtrar_ilhas(self, caminho_raster, area_min_ha, classe_florestal, feedback):
        """Filtro de "ilhas" (peneira/sieve do GDAL): manchas menores que a área
        mínima são absorvidas pela classe vizinha de maior contato. Regras:
          - a FLORESTA (classe 1) nunca é apagada: faixas estreitas de mata
            ciliar parecem "ilhas" para a peneira, e perdê-las faria a APP sumir;
          - vizinhança em 8 direções, para que uma faixa diagonal de pixels
            conte como uma mancha só (e não como vários pedacinhos).
        Grava uma CÓPIA filtrada; o raster original não é alterado. Em qualquer
        falha, segue com o raster original e avisa."""
        try:
            area_pixel_ha = self._tamanho_pixel_ha(caminho_raster)
            limiar = int(math.ceil(area_min_ha / area_pixel_ha))
            if limiar <= 1:
                feedback.pushInfo("Área mínima de ilha menor que 1 pixel: nada a filtrar.")
                return caminho_raster

            destino = QgsProcessingUtils.generateTempFilename("classes_filtradas.tif")
            origem = gdal.Open(caminho_raster)
            original = origem.GetRasterBand(1).ReadAsArray()
            copia = gdal.GetDriverByName("GTiff").CreateCopy(destino, origem)
            origem = None
            banda = copia.GetRasterBand(1)
            # máscara de validade: pixels sem dado (nodata) não entram na peneira
            resultado = gdal.SieveFilter(banda, banda.GetMaskBand(), banda, limiar, 8)
            if resultado not in (0, None):
                raise RuntimeError(f"SieveFilter retornou {resultado}")

            # devolve a floresta que a peneira tenha absorvido
            filtrada = banda.ReadAsArray()
            restaurar = (original == classe_florestal) & (filtrada != classe_florestal)
            n_restaurados = int(restaurar.sum())
            if n_restaurados:
                filtrada[restaurar] = classe_florestal
                banda.WriteArray(filtrada)
            copia.FlushCache()
            copia = None

            feedback.pushInfo(
                f"Filtro de ilhas aplicado: manchas < {area_min_ha} ha "
                f"(< {limiar} pixels) absorvidas pela classe vizinha. A floresta foi "
                f"preservada ({n_restaurados} pixels que a peneira apagaria foram "
                f"devolvidos).")
            return destino
        except Exception as e:
            feedback.pushWarning(
                f"Não consegui filtrar as ilhas ({e}); seguindo com o raster original.")
            return caminho_raster

    def _tamanho_pixel_ha(self, caminho_raster):
        ds = gdal.Open(caminho_raster)
        gt = ds.GetGeoTransform()
        ds = None
        return abs(gt[1] * gt[5]) / 10000.0

    def _construir_zona_escadinha(self, caminho_raster, classe_consolidada, classe_regeneracao,
                                   context, feedback, iteracoes_suavizacao=0):
        ds = gdal.Open(caminho_raster)
        if ds is None:
            raise QgsProcessingException(f"Não consegui abrir o raster: {caminho_raster}")
        array = ds.GetRasterBand(1).ReadAsArray()
        # zona onde vale a escadinha: área consolidada (2) + floresta em
        # regeneração (5), que é tratada como se fosse área consolidada
        mascara = np.isin(array, [classe_consolidada, classe_regeneracao]).astype(np.uint8)

        caminho_mascara = QgsProcessingUtils.generateTempFilename("mascara_area_consolidada.tif")
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(caminho_mascara, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(mascara)
        out_ds.GetRasterBand(1).SetNoDataValue(0)
        out_ds.FlushCache()
        out_ds = None
        ds = None

        caminho_poligono_bruto = QgsProcessingUtils.generateTempFilename("zona_escadinha_bruta.gpkg")
        processing.run("gdal:polygonize", {
            "INPUT": caminho_mascara, "BAND": 1, "FIELD": "valor",
            "EIGHT_CONNECTEDNESS": False, "OUTPUT": caminho_poligono_bruto
        }, context=context, feedback=feedback, is_child_algorithm=True)

        camada_filtrada = processing.run("native:extractbyexpression", {
            "INPUT": caminho_poligono_bruto, "EXPRESSION": '"valor" = 1',
            "OUTPUT": "memory:zona_escadinha_filtrada"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        zona = processing.run("native:dissolve", {
            "INPUT": camada_filtrada, "OUTPUT": "memory:zona_escadinha"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

        area_base = self._area_total(zona, context)
        feedback.pushInfo(
            f"Zona consolidada (classes {classe_consolidada} e {classe_regeneracao}): "
            f"{area_base / 10000.0:.2f} ha")

        if iteracoes_suavizacao > 0:
            # arredonda os "degraus" de pixel da borda da zona consolidada
            zona_suave = processing.run("native:smoothgeometry", {
                "INPUT": zona, "ITERATIONS": iteracoes_suavizacao, "OFFSET": 0.25,
                "MAX_ANGLE": 180, "OUTPUT": "memory:zona_escadinha_suave"
            }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
            # a suavização pode criar auto-interseções (geometria inválida), e o
            # QGIS aborta as operações seguintes com geometria inválida; repara
            zona_suave = processing.run("native:fixgeometries", {
                "INPUT": zona_suave, "OUTPUT": "memory:zona_escadinha_suave_valida"
            }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]

            # trava: a suavização só deve arredondar bordas. Se a área da zona mudar
            # mais de 10%, algo deformou o polígono: usa a zona sem suavizar
            area_suave = self._area_total(zona_suave, context)
            razao = (area_suave / area_base) if area_base else 1.0
            if 0.9 <= razao <= 1.1:
                zona = zona_suave
                feedback.pushInfo(
                    f"Suavização aplicada (zona com {razao * 100:.1f}% da área original).")
            else:
                feedback.pushWarning(
                    f"A suavização mudou a área da zona consolidada em mais de 10% "
                    f"({razao * 100:.1f}% da original). Usando a zona SEM suavizar.")
        return zona

    def _poligonizar_classes(self, caminho_raster, lotes_ref, context, feedback):
        """Transforma o raster de classes em polígonos (campo 'classe'),
        recortando antes pela extensão dos lotes para vetorizar só o necessário."""
        caminho_base = caminho_raster
        try:
            camada_lotes = self._camada(lotes_ref, context)
            ext = camada_lotes.extent()
            crs_l = camada_lotes.crs()
            extensao = (f"{ext.xMinimum()},{ext.xMaximum()},"
                        f"{ext.yMinimum()},{ext.yMaximum()} [{crs_l.authid()}]")
            recorte = QgsProcessingUtils.generateTempFilename("classes_recorte.tif")
            processing.run("gdal:cliprasterbyextent", {
                "INPUT": caminho_raster, "PROJWIN": extensao, "OVERCRS": False,
                "NODATA": None, "OPTIONS": "", "DATA_TYPE": 0, "EXTRA": "",
                "OUTPUT": recorte
            }, context=context, feedback=feedback, is_child_algorithm=True)
            caminho_base = recorte
        except Exception as e:
            feedback.pushWarning(
                f"Não consegui recortar o raster pela extensão dos lotes ({e}); "
                f"vetorizando o raster inteiro (pode demorar).")

        caminho_poligonos = QgsProcessingUtils.generateTempFilename("classes_poligonizadas.gpkg")
        processing.run("gdal:polygonize", {
            "INPUT": caminho_base, "BAND": 1, "FIELD": "classe",
            "EIGHT_CONNECTEDNESS": False, "OUTPUT": caminho_poligonos
        }, context=context, feedback=feedback, is_child_algorithm=True)
        return caminho_poligonos

    @staticmethod
    def _medidor_area(crs, context):
        """None para CRS projetado (área plana da geometria); medidor
        elipsoidal para CRS geográfico (graus não servem para área)."""
        if not crs.isGeographic():
            return None
        medidor = QgsDistanceArea()
        medidor.setSourceCrs(crs, context.transformContext())
        medidor.setEllipsoid(crs.ellipsoidAcronym() or "WGS84")
        return medidor

    @staticmethod
    def _area_ha(geom, medidor):
        if geom is None or geom.isEmpty():
            return 0.0
        m2 = medidor.measureArea(geom) if medidor is not None else geom.area()
        return m2 / 10000.0

    def _tabela_classes_vetor(self, alvo_ref, poligonos_classes, prefixo, classes,
                               extras_por_fid, campo_id, classe_consolidada, context, feedback):
        """Área de cada classe pelo VETOR: interseção dos polígonos-alvo (lote
        ou APP) com o raster vetorizado e soma das áreas geométricas. O pixel
        cortado pela borda entra só na parte que está dentro do polígono."""
        alvo = self._camada(alvo_ref, context)
        medidor = self._medidor_area(alvo.crs(), context)

        inter = processing.run("native:intersection", {
            "INPUT": alvo_ref, "OVERLAY": poligonos_classes,
            "INPUT_FIELDS": ["fid_lote"], "OVERLAY_FIELDS": ["classe"],
            "OUTPUT": f"memory:inter_{prefixo}"
        }, context=context, feedback=feedback, is_child_algorithm=True)["OUTPUT"]
        camada_inter = self._camada(inter, context)

        areas = {}  # fid_lote -> {classe: ha}
        for f in camada_inter.getFeatures():
            por_classe = areas.setdefault(f["fid_lote"], {})
            classe = f["classe"]
            por_classe[classe] = por_classe.get(classe, 0.0) + self._area_ha(f.geometry(), medidor)

        campos_base = alvo.fields()
        # mantém só a coluna com o nome do lote; o resto das colunas originais
        # do lote não é repetido na tabela
        idx_manter = [i for i in [campos_base.indexFromName(campo_id)] if i >= 0]
        campos = QgsFields()
        for i in idx_manter:
            campos.append(campos_base.field(i))
        campos.append(QgsField(f"{prefixo}_area_ha", TIPO_DECIMAL))
        for n in classes:
            nome = NOMES_CLASSES.get(n, f"classe{n}")
            campos.append(QgsField(f"{prefixo}_{nome}_ha", TIPO_DECIMAL))
            campos.append(QgsField(f"{prefixo}_{nome}_pct", TIPO_DECIMAL))
        campos.append(QgsField(f"{prefixo}_sem_classe_ha", TIPO_DECIMAL))
        campos.append(QgsField(f"{prefixo}_sem_classe_pct", TIPO_DECIMAL))
        for nome, tipo in (("modulos_fiscais", TIPO_DECIMAL),
                           ("pequena_propriedade", TIPO_TEXTO),
                           ("teto_61b_ha", TIPO_DECIMAL)):
            campos.append(QgsField(nome, tipo))
        if prefixo == "app":
            # checagem do Art. 61-B (só faz sentido dentro da APP)
            campos.append(QgsField("recompor_consolidada_ha", TIPO_DECIMAL))
            campos.append(QgsField("excede_61b", TIPO_TEXTO))
            campos.append(QgsField("excesso_61b_ha", TIPO_DECIMAL))

        idx_fid = campos_base.indexFromName("fid_lote")
        feicoes = []
        for f in alvo.getFeatures():
            atributos = list(f.attributes())
            fid = atributos[idx_fid] if idx_fid >= 0 else None
            por_classe = areas.get(fid, {})
            area_total = self._area_ha(f.geometry(), medidor)

            novos = [area_total]
            soma = 0.0
            for n in classes:
                a = por_classe.get(n, 0.0)
                soma += a
                novos.append(a)
                novos.append((a / area_total * 100.0) if area_total else 0.0)
            # o que não caiu em nenhuma das classes (fora do raster, sem dado
            # ou outro código); max(...) absorve ruído de arredondamento
            sem = max(area_total - soma, 0.0)
            novos.append(sem)
            novos.append((sem / area_total * 100.0) if area_total else 0.0)

            info = extras_por_fid.get(fid)
            if info is not None:
                novos.extend([round(info["modulos_fiscais"], 4),
                              "sim" if info["pequena_propriedade"] else "nao",
                              info["teto_61b_ha"]])
            else:
                novos.extend([None, None, None])
            if prefixo == "app":
                novos.extend(self._avaliar_61b(por_classe.get(classe_consolidada, 0.0), info))

            nova = QgsFeature(campos)
            g = f.geometry()
            if g is not None and not g.isEmpty():
                if not g.isMultipart():
                    g.convertToMultiType()
                nova.setGeometry(g)
            nova.setAttributes([atributos[i] for i in idx_manter] + novos)
            feicoes.append(nova)
        return campos, feicoes

    @staticmethod
    def _avaliar_61b(recompor_ha, info):
        """Art. 61-B: compara a área de ÁREA CONSOLIDADA que cai dentro da APP
        (a faixa reduzida que precisa ser recomposta) com o teto do lote.
        Devolve (recompor_ha, excede_61b, excesso_ha)."""
        if info is None:
            return [recompor_ha, None, None]
        if info["modulos_fiscais"] > 10:
            return [recompor_ha, "nao se aplica", None]
        teto = info["teto_61b_ha"]
        if teto is None:  # 4 a 10 MF: o inciso III do Art. 61-B foi vetado
            return [recompor_ha, "sem teto", None]
        excesso = max(recompor_ha - teto, 0.0)
        return [recompor_ha, "sim" if excesso > 0 else "nao", excesso]

    def _recortar_poligonos(self, geom, lote_geom, geografico):
        """Interseção de 'geom' com o contorno do lote, só com partes poligonais.
        Em CRS projetado descarta lascas menores que AREA_MIN_FRAGMENTO_M2.
        Devolve None se não sobrar nada."""
        if geom is None or geom.isEmpty():
            return None
        if not lote_geom.isGeosValid():
            lote_geom = lote_geom.makeValid()
        inter = geom.intersection(lote_geom)
        if inter is None or inter.isEmpty():
            return None
        partes = [p for p in inter.asGeometryCollection()
                  if p.type() == GEOM_POLIGONO]
        if not geografico:
            partes = [p for p in partes if p.area() >= AREA_MIN_FRAGMENTO_M2]
        return QgsGeometry.collectGeometry(partes) if partes else None

    def _area_total(self, ref, context):
        """Soma das áreas das feições de uma camada (unidade do CRS ao quadrado)."""
        total = 0.0
        for f in self._camada(ref, context).getFeatures():
            g = f.geometry()
            if g is not None and not g.isEmpty():
                total += g.area()
        return total

    def _camada(self, valor, context):
        """Devolve o objeto de camada a partir do id (texto) que um
        sub-algoritmo retorna quando rodado com is_child_algorithm=True: a
        camada continua pertencendo ao contexto (não ao Python), o que evita
        o crash por camada liberada fora de hora numa thread de fundo."""
        if isinstance(valor, QgsMapLayer):
            return valor
        camada = QgsProcessingUtils.mapLayerFromString(valor, context)
        if camada is None:
            raise QgsProcessingException(
                f"Não consegui acessar a camada intermediária '{valor}'.")
        return camada

    def _histograma(self, entrada, caminho_raster, prefixo, context, feedback):
        resultado = processing.run("native:zonalhistogram", {
            "INPUT_VECTOR": entrada, "INPUT_RASTER": caminho_raster, "RASTER_BAND": 1,
            "COLUMN_PREFIX": f"{prefixo}_contagem_classe",
            "OUTPUT": f"memory:hist_{prefixo}"
        }, context=context, feedback=feedback, is_child_algorithm=True)
        return self._camada(resultado["OUTPUT"], context)

    @staticmethod
    def _num(valor):
        try:
            return float(valor)
        except (TypeError, ValueError):
            return 0.0

    def _montar_tabela_classes(self, hist_layer, prefixo, classes, area_pixel_ha,
                                extras_por_fid, campo_id, classe_consolidada):
        """Monta (campos, feições) da tabela final: campos do histograma +
        área (ha) e % de cada classe + módulos fiscais / pequena propriedade /
        teto do 61-B. Tudo em Python, sem editar camada nenhuma."""
        campos_base = hist_layer.fields()
        # mantém só a coluna com o nome do lote + as colunas de contagem de
        # pixels geradas pelo histograma; as demais colunas originais do lote
        # não são repetidas na tabela
        nomes_manter = [campo_id] + [
            fld.name() for fld in campos_base
            if fld.name().startswith(f"{prefixo}_contagem_classe")]
        idx_manter = [campos_base.indexFromName(n) for n in nomes_manter
                      if campos_base.indexFromName(n) >= 0]
        campos = QgsFields()
        for i in idx_manter:
            campos.append(campos_base.field(i))
        for n in classes:
            nome = NOMES_CLASSES.get(n, f"classe{n}")
            campos.append(QgsField(f"{prefixo}_{nome}_ha", TIPO_DECIMAL))
            campos.append(QgsField(f"{prefixo}_{nome}_pct", TIPO_DECIMAL))
        for nome, tipo in (("modulos_fiscais", TIPO_DECIMAL),
                           ("pequena_propriedade", TIPO_TEXTO),
                           ("teto_61b_ha", TIPO_DECIMAL)):
            campos.append(QgsField(nome, tipo))
        if prefixo == "app":
            # checagem do Art. 61-B (só faz sentido dentro da APP)
            campos.append(QgsField("recompor_consolidada_ha", TIPO_DECIMAL))
            campos.append(QgsField("excede_61b", TIPO_TEXTO))
            campos.append(QgsField("excesso_61b_ha", TIPO_DECIMAL))

        idx_fid = campos_base.indexFromName("fid_lote")
        idx_cont = {n: campos_base.indexFromName(f"{prefixo}_contagem_classe{n}")
                    for n in classes}

        feicoes = []
        for f in hist_layer.getFeatures():
            atributos = list(f.attributes())
            contagens = {n: (self._num(atributos[i]) if i >= 0 else 0.0)
                         for n, i in idx_cont.items()}
            total = sum(contagens.values())
            novos = []
            for n in classes:
                novos.append(contagens[n] * area_pixel_ha)
                novos.append((contagens[n] / total * 100.0) if total else 0.0)

            info = extras_por_fid.get(atributos[idx_fid]) if idx_fid >= 0 else None
            if info is not None:
                novos.extend([round(info["modulos_fiscais"], 4),
                              "sim" if info["pequena_propriedade"] else "nao",
                              info["teto_61b_ha"]])
            else:
                novos.extend([None, None, None])
            if prefixo == "app":
                novos.extend(self._avaliar_61b(
                    contagens.get(classe_consolidada, 0.0) * area_pixel_ha, info))

            nova = QgsFeature(campos)
            g = f.geometry()
            if g is not None and not g.isEmpty():
                if not g.isMultipart():
                    g.convertToMultiType()
                nova.setGeometry(g)
            nova.setAttributes([atributos[i] for i in idx_manter] + novos)
            feicoes.append(nova)
        return campos, feicoes
