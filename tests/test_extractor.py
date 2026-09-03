from __future__ import annotations

from pathlib import Path

import diskcache

from src.extractor import (
    MCUExtractSpec,
    SpecExtractor,
    extract_by_rules,
    extract_by_selectors,
    extract_specs_many,
)


def test_empty_html_returns_unknown_and_low_confidence(tmp_path) -> None:
    extractor = SpecExtractor(cache=diskcache.Cache(str(tmp_path / "cache")))
    spec = extractor.extract_specs("<html><body></body></html>")
    assert spec.part_number == "unknown"
    assert spec.core_arch == "unknown"
    assert spec.flash_kb == 0
    assert spec.price_rub == 0
    assert spec.llm_confidence < 0.5
    assert spec.needs_review is True


def test_empty_html_is_cached(tmp_path) -> None:
    extractor = SpecExtractor(cache=diskcache.Cache(str(tmp_path / "cache")))
    first = extractor.extract_specs("<html><body></body></html>")
    second = extractor.extract_specs("<html><body></body></html>")
    assert first == second


def test_blank_fields_become_unknown_and_zero() -> None:
    spec = MCUExtractSpec.model_validate(
        {
            "part_number": "  ",
            "core_arch": None,
            "flash_kb": None,
            "llm_confidence": 0.9,
        }
    )
    assert spec.part_number == "unknown"
    assert spec.core_arch == "unknown"
    assert spec.flash_kb == 0
    assert spec.llm_confidence < 0.5
    assert spec.needs_review is True


def test_confidence_below_threshold_needs_review() -> None:
    spec = MCUExtractSpec(part_number="STM32F103C8T6", llm_confidence=0.79)
    assert spec.needs_review is True
    ok = MCUExtractSpec(part_number="STM32F103C8T6", llm_confidence=0.8)
    assert ok.needs_review is False


def test_extract_by_rules_our_catalog(our_catalog_html: str) -> None:
    specs = extract_by_rules(our_catalog_html)
    parts = {item.part_number: item for item in specs}
    assert set(parts) == {"STM32F103C8T6", "STM32F411CEU6"}
    f103 = parts["STM32F103C8T6"]
    assert f103.core_arch == "Cortex-M3"
    assert f103.flash_kb == 64
    assert f103.ram_kb == 20
    assert f103.freq_mhz == 72
    assert f103.package == "LQFP48"
    assert f103.pins_count == 48
    assert f103.price_rub == 210
    assert f103.llm_confidence == 1.0
    assert f103.needs_review is False


def test_extract_by_rules_platan_catalog(platan_catalog_html: str) -> None:
    specs = extract_by_rules(platan_catalog_html)
    parts = {item.part_number: item for item in specs}
    assert {"STM32F103C8T6", "STM32F411CEU6", "STM32F103CBT6", "STM32F407VGT6", "STM32F107VCT6"} <= set(parts)
    f103 = parts["STM32F103C8T6"]
    assert f103.core_arch == "ARM Cortex M3"
    assert f103.flash_kb == 64
    assert f103.ram_kb == 20
    assert f103.freq_mhz == 72
    assert f103.package == "LQFP-48"
    assert f103.pins_count == 48
    assert f103.price_rub == 150
    assert f103.stock_qty == 1613
    assert f103.brand == "STMicroelectronics"
    assert "-40" in f103.temp_range
    assert f103.nomenclature_id == "2015361529"
    f411 = parts["STM32F411CEU6"]
    assert f411.core_arch == "ARM Cortex M4"
    assert f411.flash_kb == 512
    assert f411.ram_kb == 0
    assert f411.freq_mhz == 0
    assert f411.package == "UFQFPN-48"
    assert f411.price_rub == 473.77
    assert f411.stock_qty == 1348


def test_extract_by_rules_chipdip_catalog(chipdip_catalog_html: str) -> None:
    specs = extract_by_rules(chipdip_catalog_html)
    parts = {item.part_number: item for item in specs}
    assert {"STM32F103C8T6", "STM32F411CEU6", "STM32F103CBT6", "STM32F407VGT6", "STM32F107VCT6"} <= set(parts)
    f103 = parts["STM32F103C8T6"]
    assert f103.core_arch == "Cortex-M3"
    assert f103.flash_kb == 64
    assert f103.ram_kb == 0
    assert f103.freq_mhz == 72
    assert f103.price_rub == 160
    assert f103.stock_qty == 3646
    f411 = parts["STM32F411CEU6"]
    assert f411.flash_kb == 512
    assert f411.ram_kb == 128
    assert f411.freq_mhz == 100
    assert f411.price_rub == 450
    assert f411.stock_qty == 509


def test_extract_by_rules_promelec_catalog(promelec_catalog_html: str) -> None:
    specs = extract_by_rules(promelec_catalog_html)
    parts = {item.part_number: item for item in specs}
    assert {"STM32F103C8T6", "STM32F411CEU6", "STM32F103CBT6", "STM32F407VGT6", "STM32F107VCT6"} <= set(parts)
    f103 = parts["STM32F103C8T6"]
    assert f103.core_arch == "ARM Cortex-M3"
    assert f103.flash_kb == 64
    assert f103.ram_kb == 20
    assert f103.freq_mhz == 72
    assert f103.price_rub == 175.04
    assert f103.stock_qty == 31542
    f411 = parts["STM32F411CEU6"]
    assert f411.core_arch == "ARM Cortex-M4"
    assert f411.flash_kb == 512
    assert f411.ram_kb == 0
    assert f411.freq_mhz == 100
    assert f411.price_rub == 468.93
    assert f411.stock_qty == 1348


def test_extract_specs_many_prefers_rules(our_catalog_html: str) -> None:
    class Boom:
        def extract_specs(self, html: str) -> MCUExtractSpec:
            raise AssertionError("LLM must not run when articles exist")

    specs = extract_specs_many(our_catalog_html, extractor=Boom())  # type: ignore[arg-type]
    assert len(specs) == 2


def test_extract_specs_many_skips_unknown_llm() -> None:
    class Dummy:
        def extract_specs(self, html: str) -> MCUExtractSpec:
            return MCUExtractSpec()

    assert extract_specs_many("<html><p>catalog listing</p></html>", extractor=Dummy()) == []


def test_extract_chipdip_listing_row_not_neighbor() -> None:
    html = (
        Path(__file__).resolve().parent / "fixtures" / "chipdip_listing_f103.html"
    ).read_text(encoding="utf-8")
    specs = extract_by_selectors(html)
    parts = {item.part_number: item for item in specs}
    assert set(parts) == {"STM32F103C8T6"}
    f103 = parts["STM32F103C8T6"]
    assert f103.core_arch == "Cortex-M3"
    assert f103.flash_kb == 64
    assert f103.ram_kb == 0
    assert f103.freq_mhz == 72
    assert f103.package == "LQFP-48"
    assert f103.price_rub == 160
    assert f103.stock_qty == 3646


def test_extract_promelec_product_in_stock_price() -> None:
    html = (
        Path(__file__).resolve().parent / "fixtures" / "promelec_product_f103.html"
    ).read_text(encoding="utf-8")
    specs = extract_by_selectors(html)
    assert len(specs) == 1
    f103 = specs[0]
    assert f103.part_number == "STM32F103C8T6"
    assert f103.core_arch == "ARM Cortex-M3"
    assert f103.flash_kb == 64
    assert f103.ram_kb == 20
    assert f103.freq_mhz == 72
    assert f103.package == "LQFP48"
    assert f103.price_rub == 175.04
    assert f103.stock_qty == 31542


def test_extract_promelec_listing_skips_from_price() -> None:
    html = (
        Path(__file__).resolve().parent / "fixtures" / "promelec_listing_f103.html"
    ).read_text(encoding="utf-8")
    specs = extract_by_selectors(html)
    assert len(specs) == 1
    f103 = specs[0]
    assert f103.part_number == "STM32F103C8T6"
    assert f103.stock_qty == 31542
    assert f103.flash_kb == 64
    assert f103.ram_kb == 20
    assert f103.price_rub == 0


def test_extract_specs_many_uses_selectors_without_articles() -> None:
    html = (
        Path(__file__).resolve().parent / "fixtures" / "chipdip_listing_f103.html"
    ).read_text(encoding="utf-8")

    class Boom:
        def extract_specs(self, html: str) -> MCUExtractSpec:
            raise AssertionError("LLM must not run when selectors match")

    specs = extract_specs_many(html, extractor=Boom())  # type: ignore[arg-type]
    assert specs[0].stock_qty == 3646
