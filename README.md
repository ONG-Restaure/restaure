# Restaure — Plugin para QGIS

![QGIS](https://img.shields.io/badge/QGIS-3.x-589632?logo=qgis&logoColor=white)
![Plataforma](https://img.shields.io/badge/plataforma-Processing-blue)
[![Licença: GPL v3](https://img.shields.io/badge/licença-GPL--3.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

Ferramentas para o QGIS que agilizam projetos de **georreferenciamento de imóveis rurais** e de **regularização ambiental** no Brasil, em conformidade com as normas técnicas do **INCRA** e com o **Código Florestal (Lei nº 12.651/2012)**.

O Restaure reúne algoritmos de geoprocessamento na **Caixa de Ferramentas de Processamento** do QGIS, no grupo **Restaure**, e foi pensado para crescer: novas ferramentas serão adicionadas ao longo do tempo.

---

## Sumário

- [Ferramentas disponíveis](#ferramentas-disponíveis)
- [Instalação e uso](#instalação-e-uso)
- [Requisitos gerais](#requisitos-gerais)
- [Detalhes das ferramentas](#detalhes-das-ferramentas)
  - [Gerador de APP](#gerador-de-app)
  - [Gerador de Nascentes](#gerador-de-nascentes)
- [Fluxo de trabalho sugerido](#fluxo-de-trabalho-sugerido)
- [Aviso importante](#aviso-importante)
- [Autores e contato](#autores-e-contato)
- [Licença](#licença)

---

## Ferramentas disponíveis

| Ferramenta | Finalidade | Localização na Caixa de Ferramentas |
|---|---|---|
| [Gerador de Nascentes](#gerador-de-nascentes) | Identifica possíveis nascentes a partir de redes de drenagem e aponta suspeitas de desconexão e possíveis fozes. | Restaure → Gerador de Nascentes |
| [Gerador de APP](#gerador-de-app) | Delimita Áreas de Preservação Permanente (APP) e analisa classes ambientais por lote. | Restaure → Gerador de APP |

> Novas ferramentas serão acrescentadas a esta tabela conforme forem publicadas.

---

## Instalação e uso

1. No QGIS, abra **Complementos → Gerenciar e Instalar Complementos**, procure por **Restaure** e clique em **Instalar**.
2. Abra a **Caixa de Ferramentas de Processamento** (`Ctrl+Alt+T`).
3. Localize o grupo **Restaure** e a ferramenta desejada.
4. Selecione as camadas de entrada, ajuste os parâmetros e execute o algoritmo.

> Na primeira execução de algumas ferramentas (como o Gerador de APP) pode ser necessária conexão com a internet para obter recursos auxiliares que ainda não estejam disponíveis localmente.

---

## Requisitos gerais

- **QGIS** com o provedor de Processamento habilitado.
- Camadas em **sistema de coordenadas projetado, com unidades em metros**, cobrindo toda a área de estudo.
- Conferência prévia da estrutura dos dados e dos atributos exigidos por cada ferramenta.

Os dados de entrada específicos de cada ferramenta estão descritos nas seções abaixo.

---

## Detalhes das ferramentas

### Gerador de APP

Algoritmo que auxilia na delimitação de **Áreas de Preservação Permanente (APP)** e na análise de classes ambientais por lote. Combina dados hidrográficos, limites dos lotes e um raster temático para produzir camadas geoespaciais e indicadores de apoio ao diagnóstico ambiental.

**Funcionalidades**

- Delimitação de APPs associadas a rios, lagos, lagoas e nascentes.
- Consideração de informações ambientais classificadas em raster.
- Cálculo de áreas e indicadores ambientais por lote e por APP.
- Identificação de informações relacionadas a módulos fiscais e à recomposição ambiental.
- Geração de camadas vetoriais para visualização e análise no QGIS.

**Dados de entrada**

| Dado | Obrigatório | Observação |
|---|---|---|
| Camada de lotes rurais | Sim | Limites dos imóveis/lotes. |
| Hidrografia em linhas | Sim | Rios e córregos. |
| Hidrografia em polígonos | Opcional | Lagos, lagoas e corpos d'água. |
| Camada de nascentes | Opcional | Quando disponível (pode ser gerada pelo Gerador de Nascentes). |
| Raster de classes ambientais | Sim | Raster temático usado na análise por classe. |
| Tabela de módulos fiscais do INCRA | Sim | Obtida pelo plugin ou fornecida pelo usuário. |

**Resultados**

Camadas com informações ambientais por lote e por APP, incluindo áreas e indicadores derivados das classes temáticas. Uma camada adicional de **APP integral** pode ser gerada para visualização e comparação.

**Observações**

Antes de utilizar os resultados em processos oficiais, confira os dados, os parâmetros e a legislação aplicável, inclusive as orientações do **Programa de Regularização Ambiental (PRA)** do estado.

---

### Gerador de Nascentes

Ferramenta que identifica automaticamente **possíveis nascentes** a partir de redes de drenagem. Analisa as extremidades das linhas hidrográficas e pode utilizar lagos, áreas de abrangência e modelos de elevação para apoiar a identificação de pontos que precisam de conferência.

**Funcionalidades**

- Geração automática de pontos em extremidades desconectadas da rede de drenagem.
- Identificação de possíveis falhas de conexão na hidrografia.
- Consideração de lagos, lagoas e represas para evitar classificar determinadas conexões como nascentes.
- Uso opcional de modelos digitais de elevação ou superfície para indicar possíveis fozes.
- Geração de camadas separadas para nascentes, suspeitas de desconexão e possíveis fozes.

**Dados de entrada**

| Dado | Obrigatório | Observação |
|---|---|---|
| Hidrografia em linhas | Sim | Rios e córregos. |
| Lagos, lagoas e represas | Opcional | Camada poligonal. |
| Área de abrangência | Opcional | Camada poligonal para limitar a área de análise. |
| MDE ou MDS | Opcional | Raster de elevação, usado quando o modo de análise com elevação é selecionado. |

**Resultados**

Camadas de pontos com as nascentes identificadas, as extremidades que podem indicar falhas de conexão (suspeitas) e, quando aplicável, as possíveis fozes para revisão.

**Observações**

As extremidades desconectadas da hidrografia **nem sempre representam nascentes reais**: também podem resultar de erros de digitalização, linhas incompletas ou rios cortados nos dados de entrada. Revise a camada de suspeitas e, quando utilizado, confira o resultado de possíveis fozes.

---

## Fluxo de trabalho sugerido

As ferramentas podem ser usadas de forma independente, mas se complementam:

1. **Prepare e valide a hidrografia** (linhas, lagos, área de abrangência).
2. Execute o **Gerador de Nascentes** e revise as camadas de nascentes, suspeitas e fozes.
3. Use a camada de nascentes validada, junto com lotes, hidrografia e raster de classes, no **Gerador de APP**.
4. Confira os resultados e a legislação aplicável antes de utilizá-los em produtos técnicos ou processos oficiais.

---

## Aviso importante

As ferramentas do Restaure são de **apoio técnico**. Elas **não substituem** a avaliação de profissional habilitado, a validação técnica dos dados de entrada nem a análise do órgão ambiental ou fundiário competente. Verifique sempre dados, parâmetros e legislação vigente (normas técnicas do INCRA, Lei nº 12.651/2012 e regras estaduais do PRA) antes de utilizar os resultados.

Para instruções detalhadas sobre atributos, parâmetros, regras de cálculo, fluxo de revisão e solução de problemas, consulte o manual completo ou o material de treinamento de cada ferramenta.

---

## Autores e contato

O Restaure é desenvolvido por:

| Autor | LinkedIn |
|---|---|
| **Dra. Ranieli dos Anjos de Souza** | [![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/ranieli-dos-anjos-de-souza-5a291b32/) |
| **Dr. Valdir Moura** | [![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/valdir-moura-181a7b14) |
| **Maik Rodrigues** | [![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/maikrodriguess/) |
| **Mychelle Novais** | [![LinkedIn](https://img.shields.io/badge/LinkedIn-0A66C2?logo=linkedin&logoColor=white)](https://www.linkedin.com/in/mychelle-novais/) |

**Contato:** [restaurear@gmail.com](mailto:restaurear@gmail.com)

---

## Licença

Este projeto é distribuído sob a licença **GNU General Public License v3.0 (GPL-3.0)**. Consulte o arquivo [LICENSE](LICENSE) para o texto completo.
