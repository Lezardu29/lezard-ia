"""Exports des données de consommation : CSV, JSON, PDF (tâche 6.5).

- CSV : produit au fil de l'eau (aucun chargement complet en mémoire). Par défaut au format
  d'Excel français : séparateur « ; », virgule décimale, BOM UTF-8 ; locale=en -> « , » et point.
- JSON : métadonnées, résumé, économies puis lignes, également en flux.
- PDF : rapport A4 (indicateurs, origine des économies, modèles, détail par jour) via fpdf2,
  polices standard en Windows-1252 (accents et « € » sans police embarquée).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, UTC
from collections.abc import Iterable, Iterator

from fpdf import FPDF

COLUMNS = [  # (colonne SQL, en-tête)
    ("ts", "date_utc"),
    ("key_id", "cle_id"),
    ("key_name", "cle"),
    ("project_id", "projet"),
    ("plan", "plan"),
    ("endpoint", "endpoint"),
    ("status", "statut"),
    ("error_code", "code_erreur"),
    ("provider", "fournisseur"),
    ("model", "modele"),
    ("hosting", "hebergement"),
    ("mode", "mode"),
    ("effort", "effort"),
    ("task", "tache"),
    ("prompt_tokens", "tokens_entree"),
    ("completion_tokens", "tokens_sortie"),
    ("cached_tokens", "tokens_cache"),
    ("total_tokens", "tokens_total"),
    ("cost_usd", "cout_reel_usd"),
    ("cost_eur", "cout_reel_eur"),
    ("billed_eur", "facture_eur"),
    ("fx_rate", "taux_usd_eur"),
    ("saved_usd", "economie_cache_usd"),
    ("latency_ms", "latence_ms"),
    ("ttft_ms", "premier_token_ms"),
    ("cache_hit", "cache_reponse"),
    ("usage_estimated", "usage_estime"),
    ("fallback_from", "fallback_depuis"),
    ("agents", "agents"),
]
NUMERIC = {"cost_usd", "cost_eur", "billed_eur", "fx_rate", "saved_usd"}


def enrich(row: dict, key_names: dict[str, str]) -> dict:
    row = dict(row)
    row["key_name"] = key_names.get(row.get("key_id") or "", "")
    rate = row.get("fx_rate") or 0
    row["cost_eur"] = round((row.get("cost_usd") or 0) * rate, 8)
    return row


def _cell(col: str, value, decimal: str) -> str:
    if value is None:
        return ""
    if col == "ts":
        return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, bool) or col in ("cache_hit", "usage_estimated"):
        return "oui" if value else "non"
    if col in NUMERIC and isinstance(value, (int, float)):
        s = f"{value:.8f}".rstrip("0").rstrip(".") or "0"
        return s.replace(".", decimal)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value  # texte saisi par un client (modèle demandé, nom de clé…) : jamais une formule Excel
    return str(value)


def csv_stream(rows: Iterable[dict], key_names: dict[str, str], locale: str = "fr") -> Iterator[str]:
    sep, decimal = (";", ",") if locale == "fr" else (",", ".")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, lineterminator="\r\n")
    yield "﻿"  # BOM : accents corrects dans Excel
    w.writerow([h for _, h in COLUMNS])
    n = 0
    for row in rows:
        r = enrich(row, key_names)
        w.writerow([_cell(c, r.get(c), decimal) for c, _ in COLUMNS])
        n += 1
        if n % 500 == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue()


def json_stream(meta: dict, rows: Iterable[dict], key_names: dict[str, str]) -> Iterator[str]:
    head = json.dumps(meta, ensure_ascii=False)
    yield head[:-1] + ', "rows": ['
    first = True
    for row in rows:
        r = enrich(row, key_names)
        out = {h: r.get(c) for c, h in COLUMNS}
        out["date_utc"] = _cell("ts", r["ts"], ".")
        yield ("" if first else ",") + json.dumps(out, ensure_ascii=False)
        first = False
    yield "]}"


# ---------- PDF ----------


def _eur(v: float | None) -> str:
    if v is None:
        return "–"
    s = f"{abs(v):,.4f}" if abs(v) < 1 else f"{abs(v):,.2f}"
    s = s.replace(",", " ").replace(".", ",")
    if abs(v) < 1:
        s = s.rstrip("0")
        if s.endswith(","):
            s += "00"
        elif len(s.split(",")[1]) < 2:
            s += "0"
    return ("-" if v < 0 else "") + s + " €"


def _int(v) -> str:
    return f"{int(v or 0):,}".replace(",", " ")


def _ms(v) -> str:
    if v is None:
        return "–"
    return f"{v} ms" if v < 1000 else f"{v / 1000:.1f}".replace(".", ",") + " s"


FX_SOURCES = {"default": "taux par défaut", "BCE": "Banque centrale européenne"}


class Report(FPDF):
    def __init__(self, title: str, subtitle: str):
        super().__init__(format="A4")
        self.core_fonts_encoding = "windows-1252"  # « € », accents, tirets
        self.title_text, self.subtitle = title, subtitle
        self.set_auto_page_break(True, margin=18)
        self.set_margins(16, 16, 16)

    def header(self):
        self.set_font("helvetica", "B", 15)
        self.set_text_color(23, 23, 28)
        self.cell(0, 8, self.title_text, new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "", 9.5)
        self.set_text_color(107, 107, 118)
        self.cell(0, 5, self.subtitle, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(227, 227, 232)
        self.line(16, self.get_y() + 2, 194, self.get_y() + 2)
        self.ln(6)

    def footer(self):
        self.set_y(-12)
        self.set_font("helvetica", "", 8)
        self.set_text_color(140, 140, 150)
        self.cell(0, 5, f"Smart AI Gateway · page {self.page_no()}/{{nb}}", align="C")

    def section(self, title: str):
        self.ln(3)
        self.set_font("helvetica", "B", 11.5)
        self.set_text_color(23, 23, 28)
        self.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
        self.set_font("helvetica", "", 9.5)

    def kpis(self, items: list[tuple[str, str, str]]):
        w = (self.w - self.l_margin - self.r_margin - 3 * 4) / 4
        y = self.get_y()
        for i, (label, value, sub) in enumerate(items):
            x = self.l_margin + i * (w + 4)
            self.set_fill_color(247, 247, 248)
            self.set_draw_color(227, 227, 232)
            self.rect(x, y, w, 22, style="DF", round_corners=True, corner_radius=2)
            self.set_xy(x + 3, y + 2.5)
            self.set_font("helvetica", "", 7.5)
            self.set_text_color(107, 107, 118)
            self.cell(w - 6, 4, label.upper())
            self.set_xy(x + 3, y + 7.5)
            self.set_font("helvetica", "B", 13)
            self.set_text_color(23, 23, 28)
            self.cell(w - 6, 7, value)
            self.set_xy(x + 3, y + 15)
            self.set_font("helvetica", "", 7.5)
            self.set_text_color(107, 107, 118)
            self.cell(w - 6, 4, sub)
        self.set_y(y + 26)
        self.set_text_color(23, 23, 28)

    def table_rows(self, headers: list[str], rows: list[list[str]], widths: tuple, align_right_from: int = 1):
        self.set_font("helvetica", "", 9)
        align = ["LEFT"] * align_right_from + ["RIGHT"] * (len(headers) - align_right_from)
        with self.table(
            col_widths=widths,
            text_align=align,
            line_height=6,
            width=sum(widths),
            align="LEFT",
            borders_layout="HORIZONTAL_LINES",
            first_row_as_headings=True,
        ) as t:
            head = t.row()
            for h in headers:
                head.cell(h)
            for r in rows:
                row = t.row()
                for c in r:
                    row.cell(c)


def pdf_report(
    *,
    scope_label: str,
    period_label: str,
    summary: dict,
    savings: dict | None,
    models: list[dict],
    days: list[dict],
    fx: dict,
) -> bytes:
    generated = datetime.now(UTC).strftime("%d/%m/%Y %H:%M UTC")
    pdf = Report("Rapport de consommation", f"{scope_label} · {period_label} · généré le {generated}")
    pdf.alias_nb_pages()
    pdf.add_page()
    s = summary
    sv_value = _eur(savings["saved_eur"]) if savings else "–"
    sv_sub = (
        f"{str(savings['saved_pct']).replace('.', ',')} % vs {savings['baseline_model']}"
        if savings
        else "référence sans prix"
    )
    pdf.kpis(
        [
            ("Dépense totale", _eur(s["spend_eur"]), f"{_int(s['requests'])} requêtes"),
            ("Tokens", _int(s["tokens"]["total"]), f"entrée {_int(s['tokens']['prompt'])}"),
            ("Économies", sv_value, sv_sub),
            ("Latence moyenne", _ms(s["avg_latency_ms"]), f"{_int(s['errors'])} erreur(s)"),
        ]
    )
    if savings:
        pdf.section("Origine des économies")
        b = savings["breakdown_eur"]
        pdf.table_rows(
            ["Source", "Économie"],
            [
                ["Modèles locaux", _eur(b["local_models"])],
                ["Cloud gratuit", _eur(b.get("free_cloud", 0.0))],
                ["Routage intelligent", _eur(b["smart_routing"])],
                ["Prompt caching", _eur(b["prompt_caching"])],
                ["Cache de réponses", _eur(b["response_cache"])],
                ["Total", _eur(savings["saved_eur"])],
            ],
            widths=(70, 30),
        )
    if models:
        pdf.section("Modèles les plus coûteux")
        pdf.table_rows(
            ["Modèle", "Requêtes", "Coût réel", "Économie"],
            [[m["model"], _int(m["requests"]), _eur(m["cost_eur"]), _eur(m["saved_eur"])] for m in models[:15]],
            widths=(80, 25, 30, 30),
        )
    active = [d for d in days if d["requests"]]
    if active:
        pdf.section("Détail par jour")
        pdf.table_rows(
            ["Jour", "Requêtes", "Coût réel", "Référence"],
            [[d["label"], _int(d["requests"]), _eur(d["cost_eur"]), _eur(d.get("baseline_eur"))] for d in active],
            widths=(50, 30, 35, 35),
        )
    pdf.ln(4)
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(107, 107, 118)
    rate = str(fx["usd_eur"]).replace(".", ",")
    pdf.multi_cell(
        0,
        4,
        f"Montants convertis au taux USD->EUR de chaque requête (taux actuel {rate}, "
        f"source : {FX_SOURCES.get(fx['source'], fx['source'])}). Coût réel = montant payé aux "
        "fournisseurs ; les économies sont "
        "nettes, comparées à l'envoi de toutes les requêtes au modèle de référence.",
    )
    return bytes(pdf.output())
