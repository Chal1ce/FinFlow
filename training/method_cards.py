"""Method attribution and explicit adaptation boundaries embedded in generated manifests."""

CARDS = {
    "doremi": {
        "paper": "https://arxiv.org/abs/2305.10429",
        "upstream": "https://github.com/sangmichaelxie/doremi",
        "inspected_commit": "7cde52d1848737aa967ecbdb9e643cf334de160d",
        "adaptation": "local small decoder; stratified all-domain batches; tokenwise clipped excess and averaged exponentiated weights",
        "not_reproduced": "original architecture, distributed training scale and benchmark results",
    },
    "regmix": {
        "paper": "https://arxiv.org/abs/2407.01492",
        "upstream": "https://github.com/sail-sg/regmix",
        "inspected_commit": "dd9d1c3b2d7c1756b1a90f0ad7603068e9856cc6",
        "adaptation": "local proxy sweep, LightGBM, held-out mixture diagnostics, simplex search and measured confirmation",
        "not_reproduced": "original TinyLlama/Pile experiments and large-model transfer results",
    },
    "self_instruct": {
        "paper": "https://arxiv.org/abs/2212.10560",
        "upstream": "https://github.com/yizhongw/self-instruct",
        "inspected_commit": "0b26ccaa415992100fa32df62d41b994cf928e23",
        "adaptation": "curated seed instructions; one source-bound expansion; instruction screening and independent answer review",
        "not_reproduced": "unbounded cross-document self-bootstrapping seed pool and original GPT-3 dataset",
    },
    "answer_first": {
        "paper": "https://arxiv.org/abs/2308.06259",
        "adaptation": "API-based inverse questions anchored to verbatim source answers",
        "not_reproduced": "reverse-model training and iterative Humpback fine-tuning",
    },
    "evol_instruct": {
        "paper": "https://arxiv.org/abs/2304.12244",
        "adaptation": "source-constrained instruction evolution, information-gain screening and answer review",
        "not_reproduced": "open-domain input/topic expansion and original benchmark results",
    },
    "codeclm": {
        "paper": "https://arxiv.org/abs/2404.05875",
        "adaptation": "metadata encode/decode and rubric refinement; optional target-model contrastive filtering",
        "not_reproduced": "original teacher/target checkpoints, iterative fine-tuning and benchmark results",
    },
    "temperature": {
        "upstream": "https://github.com/facebookresearch/XLM",
        "inspected_commit": "cd281d32612d145c6742b4d3f048f80df8669c30",
        "adaptation": "power-law domain quotas; zero exponent is an explicit uniform-group extension",
    },
    "deita-inspired": {
        "upstream": "https://github.com/hkust-nlp/deita",
        "inspected_commit": "b279f2c329b403d2612a61e270c8d2a2eeaed6f4",
        "adaptation": "quality-times-complexity ordering within quota groups and diversity rejection",
        "not_reproduced": "original learned scorers; lexical fallback is not semantic similarity",
    },
}
