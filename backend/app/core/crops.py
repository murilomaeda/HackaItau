"""Identidade de cultura: normalização exata, sem inferir compatibilidade a partir de nomes de produtos."""

import unicodedata
from typing import Any

# culturas com referência na base de demonstração
COMMON_CROPS = (
    "soja", "milho", "cana", "cafe", "algodao", "arroz", "feijao", "trigo", "sorgo",
    "laranja", "mandioca", "cacau", "banana",
)  # fmt: skip
# Unidade em que cada cultura é negociada. Preço e produtividade precisam estar na mesma: área × produtividade × preço
# só dá reais assim. O padrão é saca; quem foge dele é declarado aqui, em código, nunca inferido de texto ou de documento.
_MEASURE = {
    "cana": ("tonelada", "t/ha"),
    "mandioca": ("tonelada", "t/ha"),
    "banana": ("tonelada", "t/ha"),
    "laranja": ("caixa", "cx/ha"),
    "algodao": ("arroba", "@/ha"),
    "cacau": ("arroba", "@/ha"),
}
_DEFAULT_MEASURE = ("saca", "sc/ha")


def expected_price_unit(crop: object) -> str:
    return "BRL/" + _MEASURE.get(normalize_crop(crop), _DEFAULT_MEASURE)[0]


def productivity_unit(crop: object) -> str:
    return _MEASURE.get(normalize_crop(crop), _DEFAULT_MEASURE)[1]


def normalize_crop(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKD", value.strip().casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def product_supports_crop(product: dict[str, Any], crop: object) -> bool:
    requested = normalize_crop(crop)
    supported = product.get("supported_crops")
    return bool(requested and isinstance(supported, list) and requested in {normalize_crop(c) for c in supported})
