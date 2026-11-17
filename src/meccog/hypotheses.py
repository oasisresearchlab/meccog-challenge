"""The five Step 1 hypotheses, copied verbatim from the challenge's hypotheses page (2026-11-17).

The statement goes into cell A2 exactly as written, so do not edit these strings.
"""

HYPOTHESES = {
    "H1": {
        "short": "Lower endosome pH",
        "statement": "In some types of non-Alzheimer's, non-aged human brain cells, for example astrocytes, "
                     "APOE4 causes reduced endosome pH compared with APOE3.",
        "seed_pmid": "29946028",
    },
    "H2": {
        "short": "Impaired receptor recycling",
        "statement": "In some types of non-Alzheimer's, non-aged human brain cells, for example astrocytes, "
                     "APOE4 causes impaired ARF6-related recycling of receptors between the cell surface and "
                     "the endosome, compared with APOE3.",
        "seed_pmid": "31641056",
    },
    "H3": {
        "short": "APOE4 aggregation in the endo-lysosome",
        "statement": "In some types of non-Alzheimer's, non-aged human brain cells, APOE4 undergoes increased "
                     "aggregation in the endo-lysosome compared with APOE3.",
        "seed_pmid": "39419029",
    },
    "H4": {
        "short": "APOE4 adhesion to recycling receptors",
        "statement": "In some types of non-Alzheimer's, non-aged human brain cells, APOE4 adheres more tightly "
                     "to some recycling receptors than APOE3, impeding recycling.",
        "seed_pmid": "30375977",
    },
    "H5": {
        "short": "Lipid metabolism and late endosome function",
        "statement": "In some types of non-Alzheimer's, non-aged human brain cells, APOE4 disruption of lipid "
                     "metabolism, compared with APOE3, results in impaired late endosome/lysosome function.",
        "seed_pmid": "37777962",
    },
}


def get(hid: str) -> dict:
    hid = hid.upper()
    if hid not in HYPOTHESES:
        raise SystemExit(f"unknown hypothesis {hid!r}; one of {', '.join(HYPOTHESES)}")
    return {"id": hid, **HYPOTHESES[hid]}
