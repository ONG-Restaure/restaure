# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProject, QgsProcessingLayerPostProcessorInterface,
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber, QgsProcessingParameterEnum,
    QgsProcessingParameterCrs, QgsProcessingParameterFeatureSink,
    QgsFields, QgsField, QgsFeature, QgsGeometry,
    QgsSpatialIndex, QgsRectangle, QgsCoordinateTransform,
)

from .compat import (
    FONTE_LINHA, FONTE_PONTO, FONTE_POLIGONO, INSERCAO_RAPIDA, NUMERO_DECIMAL,
    TIPO_DECIMAL, TIPO_INTEIRO, TIPO_TEXTO, WKB_PONTO, geometrias_iguais,
)

MODOS = ["Toda ponta solta é nascente",
         "Usar direção da linha (início = nascente)",
         "Usar MDE/MDS (marca a ponta mais baixa de cada rede como possível foz)"]


class MostrarContagem(QgsProcessingLayerPostProcessorInterface):
    """Liga 'Mostrar contagem de feições' na camada de saída."""

    def postProcessLayer(self, layer, context, feedback):
        no = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if no:
            no.setCustomProperty("showFeatureCount", True)


# referências mantidas vivas enquanto o QGIS aplica o pós-processamento
_pos_processadores = []


def retangulo(p, d):
    return QgsRectangle(p.x() - d, p.y() - d, p.x() + d, p.y() + d)


class GerarNascentes(QgsProcessingAlgorithm):

    def name(self):
        return "gerar_nascentes"

    def displayName(self):
        return "Gerador de nascentes"

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
            "<p>Gera nascentes automaticamente nas cabeceiras de drenagem, considerando "
            "as pontas soltas da rede, o sentido de digitalização ou o MDE/MDS. Trata erros "
            "comuns da base de hidrografia sem alterá-la e gera camadas para conferência "
            "do resultado.</p>"
            "<p>Elaborado por Dra. Ranieli dos Anjos de Souza e Dr. Valdir Moura</p>"
            "<p>Contato para treinamento: restaurear@gmail.com</p>"
            '<p><a href="https://wa.me/5569993512657">Fale conosco</a></p>'
        )

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "docs", "images", "icon_nascentes.png"))

    def createInstance(self):
        return GerarNascentes()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            "LINHAS", "Hidrografia (linhas)", [FONTE_LINHA]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            "POLIGONOS", "Lagos / represas (polígonos)",
            [FONTE_POLIGONO], optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            "AREA", "Área de abrangência (polígonos)",
            [FONTE_POLIGONO], optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "MARGEM", "Margem da área de abrangência (m) – inclui pontas em estradas e divisas",
            NUMERO_DECIMAL, 20.0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "TOLERANCIA", "Tolerância de conexão (m)",
            NUMERO_DECIMAL, 1.0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "SUSPEITA", "Distância para suspeita de desconexão (m)",
            NUMERO_DECIMAL, 20.0, minValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "MODO", "Como tratar a foz", MODOS, defaultValue=0))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "MDE", "MDE ou MDS (só para o modo MDE/MDS)", optional=True))
        self.addParameter(QgsProcessingParameterCrs(
            "CRS", "SRC métrico para os cálculos", "EPSG:31980"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "NASCENTES", "Nascentes", FONTE_PONTO))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "SUSPEITOS", "Suspeitos de desconexão", FONTE_PONTO))
        self.addParameter(QgsProcessingParameterFeatureSink(
            "FOZES", "Possíveis fozes (para conferência)", FONTE_PONTO))

    def processAlgorithm(self, parameters, context, feedback):
        linhas = self.parameterAsSource(parameters, "LINHAS", context)
        src_pol = self.parameterAsSource(parameters, "POLIGONOS", context)
        src_area = self.parameterAsSource(parameters, "AREA", context)
        tol = self.parameterAsDouble(parameters, "TOLERANCIA", context)
        margem = self.parameterAsDouble(parameters, "MARGEM", context)
        dist_susp = self.parameterAsDouble(parameters, "SUSPEITA", context)
        modo = self.parameterAsEnum(parameters, "MODO", context)
        mde = self.parameterAsRasterLayer(parameters, "MDE", context)
        crs = self.parameterAsCrs(parameters, "CRS", context)
        tc = context.transformContext()

        if linhas is None:
            raise QgsProcessingException("Camada de linhas inválida.")
        if modo == 2 and mde is None:
            raise QgsProcessingException("O modo MDE precisa de um raster de elevação.")
        if crs.isGeographic():
            feedback.pushWarning("O SRC escolhido está em graus; as distâncias serão em graus.")

        def em_metros(g, src):
            g = QgsGeometry(g)
            if src.sourceCrs() != crs:
                g.transform(QgsCoordinateTransform(src.sourceCrs(), crs, tc))
            return g

        # 1. Partes das linhas, sem duplicadas
        partes, origem = [], []
        idx = QgsSpatialIndex()
        duplicadas = 0
        for f in linhas.getFeatures():
            if feedback.isCanceled():
                return {}
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            g = em_metros(g, linhas)
            pls = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            for pl in pls:
                if len(pl) < 2:
                    continue
                gp = QgsGeometry.fromPolylineXY(pl)
                if any(geometrias_iguais(partes[j], gp)
                       for j in idx.intersects(gp.boundingBox())):
                    duplicadas += 1
                    continue
                i = len(partes)
                partes.append(gp)
                origem.append(f.id())
                idx.addFeature(i, gp.boundingBox())

        # 2. Lagos / represas
        poligonos, idx_pol = [], QgsSpatialIndex()
        if src_pol is not None:
            for f in src_pol.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                i = len(poligonos)
                poligonos.append(em_metros(g, src_pol))
                idx_pol.addFeature(i, poligonos[i].boundingBox())

        # 3. Área de abrangência
        area = None
        if src_area is not None:
            geoms = [em_metros(f.geometry(), src_area) for f in src_area.getFeatures()
                     if f.geometry() and not f.geometry().isEmpty()]
            if not geoms:
                raise QgsProcessingException("A área de abrangência não tem polígonos.")
            area = QgsGeometry.unaryUnion(geoms)
            if margem > 0:
                # amplia a área para cobrir estradas e faixas entre parcelas
                area = area.buffer(margem, 8)

        sobrepostas = set()

        def dist_outras(p, i, raio):
            pg = QgsGeometry.fromPointXY(p)
            menor = None
            for j in idx.intersects(retangulo(p, raio)):
                if j == i:
                    continue
                comum = partes[i].intersection(partes[j])
                if not comum.isEmpty() and comum.length() > tol:
                    # linhas sobrepostas: a ponta só NÃO conta como conectada se
                    # coincidir com uma ponta da outra (trecho duplicado na
                    # cabeceira). Se cair no meio da outra linha, está conectada.
                    sobrepostas.add(tuple(sorted((i, j))))
                    plj = partes[j].asPolyline()
                    pj_ini = QgsGeometry.fromPointXY(plj[0])
                    pj_fim = QgsGeometry.fromPointXY(plj[-1])
                    if pg.distance(pj_ini) <= tol or pg.distance(pj_fim) <= tol:
                        continue
                d = partes[j].distance(pg)
                if d <= raio and (menor is None or d < menor):
                    menor = d
            return menor

        def toca_poligono(p):
            pg = QgsGeometry.fromPointXY(p)
            return any(poligonos[j].distance(pg) <= tol
                       for j in idx_pol.intersects(retangulo(p, tol)))

        # 4. Saídas
        campos_n = QgsFields()
        campos_n.append(QgsField("linha_id", TIPO_INTEIRO))
        campos_n.append(QgsField("posicao", TIPO_TEXTO))
        campos_s = QgsFields(campos_n)
        campos_s.append(QgsField("dist", TIPO_DECIMAL))
        campos_n.append(QgsField("cota", TIPO_DECIMAL))
        campos_n.append(QgsField("possivel_foz", TIPO_TEXTO))
        sink_n, id_n = self.parameterAsSink(parameters, "NASCENTES", context,
                                            campos_n, WKB_PONTO, crs)
        sink_s, id_s = self.parameterAsSink(parameters, "SUSPEITOS", context,
                                            campos_s, WKB_PONTO, crs)
        sink_f, id_f = self.parameterAsSink(parameters, "FOZES", context,
                                            campos_n, WKB_PONTO, crs)

        cont = {"conectadas": 0, "poligono": 0, "fora_area": 0, "suspeitas": 0}
        total = len(partes) or 1

        # 5. Analisa as pontas
        # "livres" = todas as pontas que não encostam em outra linha, mesmo fora da
        # área ou em lago: são usadas para achar a foz real de cada rede (modo MDE).
        livres = []          # (ponto, parte, posicao, é_candidata)
        for i, gp in enumerate(partes):
            if feedback.isCanceled():
                return {}
            feedback.setProgress(int(80 * i / total))
            pl = gp.asPolyline()
            if pl[0] == pl[-1]:
                continue
            for p, pos in ((pl[0], "inicio"), (pl[-1], "fim")):
                if dist_outras(p, i, tol) is not None:
                    cont["conectadas"] += 1
                    continue
                if area is not None and not area.contains(QgsGeometry.fromPointXY(p)):
                    cont["fora_area"] += 1
                    livres.append((p, i, pos, False))
                    continue
                if poligonos and toca_poligono(p):
                    cont["poligono"] += 1
                    livres.append((p, i, pos, False))
                    continue
                d = dist_outras(p, i, dist_susp)
                if d is not None:
                    f = QgsFeature(campos_s)
                    f.setGeometry(QgsGeometry.fromPointXY(p))
                    f.setAttributes([origem[i], pos, round(d, 3)])
                    sink_s.addFeature(f, INSERCAO_RAPIDA)
                    cont["suspeitas"] += 1
                    livres.append((p, i, pos, False))
                    continue
                livres.append((p, i, pos, True))

        candidatas = []
        livre_para_cand = {}
        for L, (p, i, pos, cand) in enumerate(livres):
            if cand:
                livre_para_cand[L] = len(candidatas)
                candidatas.append((p, i, pos))

        # 6. Separa a foz
        foz = set()
        cotas = {}
        if modo == 1:
            foz = {k for k, c in enumerate(candidatas) if c[2] == "fim"}
        elif modo == 2:
            para_mde = QgsCoordinateTransform(crs, mde.crs(), tc)
            pai = list(range(len(partes)))

            def raiz(a):
                while pai[a] != a:
                    pai[a] = pai[pai[a]]
                    a = pai[a]
                return a

            # agrupa as linhas em redes conectadas
            for i, gp in enumerate(partes):
                pl = gp.asPolyline()
                for p in (pl[0], pl[-1]):
                    pg = QgsGeometry.fromPointXY(p)
                    for j in idx.intersects(retangulo(p, tol)):
                        if j != i and partes[j].distance(pg) <= tol:
                            pai[raiz(i)] = raiz(j)

            # em cada rede, a ponta livre mais baixa (dentro OU fora da área,
            # ou em lago) é a foz. Só é descartada se for uma nascente candidata.
            mais_baixa = {}
            sem_cota = 0
            for L, (p, i, pos, cand) in enumerate(livres):
                z, ok = mde.dataProvider().sample(para_mde.transform(p), 1)
                if not ok:
                    sem_cota += 1
                    continue
                if cand:
                    cotas[livre_para_cand[L]] = z
                r = raiz(i)
                if r not in mais_baixa or z < mais_baixa[r][1]:
                    mais_baixa[r] = (L, z)
            foz_fora = 0
            for L, _ in mais_baixa.values():
                if L in livre_para_cand:
                    foz.add(livre_para_cand[L])
                else:
                    foz_fora += 1
            feedback.pushInfo(f"Redes com foz fora da área, em lago ou suspeita: {foz_fora}")
            if sem_cota:
                feedback.pushWarning(f"{sem_cota} pontas sem valor no MDE (fora do raster ou sem dado). "
                                     "Use um MDE que cubra toda a extensão dos rios.")

        n = 0
        for k, (p, i, pos) in enumerate(candidatas):
            f = QgsFeature(campos_n)
            f.setGeometry(QgsGeometry.fromPointXY(p))
            e_foz = k in foz
            f.setAttributes([origem[i], pos, cotas.get(k), "sim" if e_foz else "nao"])
            if e_foz and sink_f is not None:
                sink_f.addFeature(f, INSERCAO_RAPIDA)
            # no modo direção a ponta final é excluída; no modo MDE/MDS ela fica
            if e_foz and modo == 1:
                continue
            sink_n.addFeature(f, INSERCAO_RAPIDA)
            n += 1
        feedback.setProgress(100)

        feedback.pushInfo(f"Partes de linha: {len(partes)} | duplicadas ignoradas: {duplicadas}")
        feedback.pushInfo(f"Pares sobrepostos ignorados: {len(sobrepostas)}")
        feedback.pushInfo(f"Pontas conectadas: {cont['conectadas']} | em lago: {cont['poligono']}"
                          f" | fora da área: {cont['fora_area']}")
        feedback.pushInfo(f"Possíveis fozes marcadas: {len(foz)} | suspeitas: {cont['suspeitas']}")
        feedback.pushInfo(f"Nascentes criadas: {n}")
        for id_saida in (id_n, id_s, id_f):
            if id_saida and context.willLoadLayerOnCompletion(id_saida):
                pp = MostrarContagem()
                _pos_processadores.append(pp)
                context.layerToLoadOnCompletionDetails(id_saida).setPostProcessor(pp)

        resultado = {"NASCENTES": id_n, "SUSPEITOS": id_s}
        if sink_f is not None:
            resultado["FOZES"] = id_f
        return resultado
