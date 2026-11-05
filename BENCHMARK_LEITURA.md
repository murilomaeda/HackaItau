# Benchmark de leitura do pedido

O benchmark original ([BENCHMARK.md](BENCHMARK.md)) começa de uma demanda já estruturada. Por isso ele nunca mediu a pergunta mais básica: **a Atena entende o pedido quando o gerente escreve de outro jeito?**

Este benchmark mede exatamente isso.

## Como funciona

- **36 pedidos fictícios** em [`interpretation_cases.json`](backend/app/evaluation/interpretation_cases.json): abreviados, por extenso ("cinquenta milhões"), com erros de digitação, em inglês, em e-mail longo, com outros valores no meio (dívida, faturamento), com dois clientes citados, com prompt injection, com finalidades e culturas variadas.
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
| Versão original (regex, antes deste trabalho) | 4/36 (11,1%) | 65,0% |
| Heurística nova | 29/36 (80,6%) | 95,0% |
| Heurística nova + LLM | rodar `make benchmark-leitura LLM=1` | |

Na versão original, só o cliente era lido em 13,9% dos casos, porque a regex exigia o padrão "cliente X solicita". Os erros que sobram na heurística nova são os que pedem compreensão de texto: separar o valor pedido da dívida ou do faturamento citados, pedidos em inglês e finalidade implícita. É para isso que serve a leitura com LLM.

## O que mudou na leitura

1. **O LLM lê o pedido inteiro e raciocina** (de onde saiu cada campo, o que é ambíguo), com a data de hoje para resolver "próxima safra".
2. **Cada campo proposto pelo LLM é validado**: o cliente precisa aparecer no texto, o valor precisa de um número correspondente, a cultura precisa estar no vocabulário e a safra no formato AAAA/AA.
3. **As duas leituras são comparadas.** Divergência em campo material vira pergunta ao analista em vez de palpite. Se as leituras discordam sobre o cliente, o cliente fica em aberto: o escopo do case nunca nasce de um palpite.
4. **Sem chave ou com erro do LLM**, a heurística segue sozinha.

## Limites

- O gabarito foi escrito por uma pessoa e cobre um tipo de pedido (crédito agro). 36 casos é melhor que 1, mas ainda é uma amostra.
- A base fictícia tem dois clientes. Os outros nomes testam a leitura, não a análise completa.
