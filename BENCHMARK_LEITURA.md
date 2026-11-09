# Benchmark de leitura do pedido

O benchmark original ([BENCHMARK.md](BENCHMARK.md)) começa de uma demanda já estruturada. Por isso ele nunca mediu a pergunta mais básica: **a Atena entende o pedido quando o gerente escreve de outro jeito?**

Este benchmark mede exatamente isso.

## Como funciona

- **48 pedidos fictícios** em [`interpretation_cases.json`](backend/app/evaluation/interpretation_cases.json): abreviados, por extenso ("cinquenta milhões"), com erros de digitação, em inglês, em e-mail longo, com outros valores no meio (dívida, faturamento), com dois clientes citados, com prompt injection, com finalidades e culturas variadas.
- **Gabarito por campo**: cliente, valor, finalidade, cultura e safra.
  - Valor esperado: acerto se a leitura for igual. O cliente é comparado sem o sufixo societário.
  - `null`: o texto não diz, então o sistema não pode inventar.
  - `"?"`: o pedido é ambíguo. Vale deixar em branco ou fazer uma pergunta de confirmação ao analista.
- Um caso só conta como certo quando **os cinco campos** estão certos.

```bash
make benchmark-leitura          # só a heurística, offline, sem chave
make benchmark-leitura LLM=1    # heurística × leitura com LLM (usa LLM_API_KEY e LLM_MODEL)
make benchmark-leitura LLM=1 ARGS="--repetitions 3"
```

## Resultados (heurística, offline)

| Leitura | Casos com os 5 campos certos | Acerto por campo |
|---|---|---|
| Versão original (regex, antes deste trabalho, nos 36 primeiros casos) | 4/36 (11,1%) | 65,0% |
| Heurística nova (36 primeiros casos) | 29/36 (80,6%) | 95,0% |
| Heurística nova (42 casos, com outras culturas e nomes em minúsculas) | 35/42 (83,3%) | 95,7% |
| Heurística nova (48 casos, com as culturas mais comuns) | 41/48 (85,4%) | 96,2% |
| Heurística nova + LLM (42 casos, gpt-4o-mini, uma repetição) | 35/42 (83,3%) | 96,7% |

Na versão original, só o cliente era lido em 13,9% dos casos, porque a regex exigia o padrão "cliente X solicita". Os erros que sobram na heurística nova são os que pedem compreensão de texto: separar o valor pedido da dívida ou do faturamento citados, pedidos em inglês e finalidade implícita. É para isso que serve a leitura com LLM. Na medição com LLM, o total de casos certos empata com a heurística, mas os erros são outros: o LLM acerta dívida, faturamento e inglês, e erra casos como bilhão, valor por extenso e safra relativa.

## O que mudou na leitura

1. **O LLM lê o pedido inteiro e raciocina** (de onde saiu cada campo, o que é ambíguo), com a data de hoje para resolver "próxima safra".
2. **Cada campo proposto pelo LLM é validado**: o cliente precisa aparecer no texto, o valor precisa de um número correspondente, a cultura precisa estar no vocabulário e a safra no formato AAAA/AA.
3. **As duas leituras são comparadas.** Divergência em campo material vira pergunta ao analista em vez de palpite. Se as leituras discordam sobre o cliente, o cliente fica em aberto: o escopo do case nunca nasce de um palpite.
4. **Sem chave ou com erro do LLM**, a heurística segue sozinha.

## Limites

- O gabarito foi escrito por uma pessoa e cobre um tipo de pedido (crédito agro). 48 casos é melhor que 1, mas ainda é uma amostra.
- A base fictícia tem catorze clientes. Os outros nomes testam a leitura, não a análise completa.

## Benchmark de conversa

A leitura só mede pedidos de crédito. Este segundo benchmark mede o que acontece com o resto: perguntas simples, nomes escritos de outro jeito e comandos fora do roteiro. O gabarito está em [`conversation_cases.json`](backend/app/evaluation/conversation_cases.json).

```bash
make benchmark-conversa   # offline: o assistente de entrada e o resolver de clientes não usam LLM
```

| Medida | O que verifica | Antes | Depois |
|---|---|---|---|
| Roteamento | Tipo de resposta e conteúdo obrigatório ou proibido | 6/32 | 32/32 |
| Busca de cliente | Variações que devem casar, e prefixos, nomes genéricos e curingas que não podem casar | 7/13 | 13/13 |

O "antes" foi medido no código anterior, já com estes casos. Parte das falhas de roteamento vinha de respostas que o código ainda não sabia dar e de políticas que ainda não existiam na base. O gabarito foi escrito junto com a correção, então 32/32 é um teste de regressão, não uma medida de generalização.
