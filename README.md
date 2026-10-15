# One-Solves-All

Reproducibility materials for *A Prompt Framework for Expert-Free LLM-Based Fully Automated Simple Power Analysis on Cryptosystems*.

The framework encodes expert SPA strategies in prompts. An LLM generates and executes analysis code, checks operation constraints, and adjusts the analysis. This release provides the processed inputs and fixed run cohort used by the revised manuscript.

## Reported results

| Method | Mean operation accuracy | Complete recovery |
|---|---:|---:|
| HCA | 54.11% | 0/250 |
| Scripted random search | 73.82% | 154/250 |
| GPT-4o | 99.36% | 235/250 |
| DeepSeek-V3.1 | 99.73% | 245/250 |

The LLM cohort contains 500 archived runs: 250 per model, with ten reported runs per trace. The 25 inputs comprise seven real recordings and eighteen simulated traces. `manifest.json` identifies every reported run, its original run number, actual settings, prompt hash, final snapshot, and the reported-cohort selection notes. Repeated analyses use a fixed recording and its reference sequence.

## Contents

- `data/`: exact processed input arrays, canonical reference labels, trace specifications, and processing descriptions.
- `prompts/`: prompts archived with the reported runs for all 25 inputs.
- `src/`: the recorded tool-calling agent and kernel service, adapted to relative paths and environment credentials.
- `runs/`: only the 500 reported LLM runs, including calls, metadata, logs, snapshots, canonical final sequences, intermediate score records, and costs.
- `baselines/`: HCA, the 7,008-configuration scripted-search menu, and the paper's baseline records.
- `results/`: precomputed numerical summaries and budget-dependent results.
- `evaluation/summarize.py`: offline reconstruction of the reported numerical results.
- `environment_recorded.json`: software versions recorded with the runs.
- `checksums.sha256`: checksums of the distributed files.

The release contains statistical results; publication plotting scripts and image files are omitted.

Canonical scoring labels are `D` for the majority operation or bit 0 and `A` for the minority operation or bit 1. RSA `S/M` labels and Kyber bits map to this notation. Analysis inputs and evaluation reference labels are supplied separately.

## Recompute numerical results

Python 3.11 or later is sufficient for the offline summary:

```console
python evaluation/summarize.py
```

The command checks all 500 final operation sequences, evaluates archived random-search prefixes, and writes summaries under `results/recomputed/`. It makes zero model API calls. The equivalent precomputed files are distributed under `results/`.

The paper's random-search budget for each trace is the ceiling of the mean of the two LLMs' average attempt counts, with a minimum of one. Random search returns the first constraint-passing labeling, otherwise the last valid labeling within the budget. Invalid configurations consume attempts. The released summary applies this rule directly to archived attempts.

HCA uses seeds 0 through 9, standardization, the fourth principal component, and K-means. FPGA uses three clusters and the sequence-based naming rule. For the other HCA traces, cluster labels are aligned with the reference labels for evaluation.

## Recorded budget results

Each budget averages the same 70 real-recording runs per method. LLM intermediate scores include offline replay, and archived original final outputs take precedence at termination. `results/curve_protocol.json` records the procedure and replay provenance. Some reconstructed intermediate states differ from the historical numerical execution. Final results use the archived final outputs.

The baseline records retain reconstruction and endpoint-scoring provenance. Some archived random-search records provide configuration, accuracy, and constraint diagnostics rather than a final label array. Their score records identify this source.

## Run a new analysis

Install dependencies:

```console
python -m pip install -r requirements.txt
```

Set `OPENROUTER_API_KEY` or `SILICONFLOW_API_KEY` in the environment. `.env.example` lists the names. The runner reads environment variables directly.

```console
python src/exp2_agent.py --model deepseek --traces RSA-SC --runs 1 --max-calls 160 --temperature 0
python src/exp2_agent.py --model gpt4o --traces RSA-SC --runs 1 --max-calls 50 --temperature 0
```

New outputs go to `local_runs/`. The distributed records remain under `runs/`. The original model identifiers, temperatures, and per-run tool-call limits are retained in each archived `run.json` and the manifest. New API responses and numerical randomness can produce different results from the archive.

Fresh baseline runs:

```console
python baselines/run.py --method hca --trace RSA-SC --seed 0
python baselines/run.py --method random --trace RSA-SC --seed 0 --budget 20
```

The paper summaries use the stored baseline records. These commands execute the exported baseline implementations on the released input.

## Data, environment, and resource records

The arrays under `data/` are the processed inputs used in the reported analyses. `data/metadata/` contains the processing descriptions for the five processed real traces. `data/traces.json` records the reference provenance, input hash, operation count, and label mapping for each trace.

The agent uses a Jupyter kernel to execute generated Python code. Run this code in an isolated environment appropriate for executable analysis scripts.

Runtime includes tool-environment startup and shutdown, API communication, model generation, and code execution. Random-search timing sums the numerical-attempt durations. Estimated API costs use cache discounts and public provider prices checked on 14 October 2026, as recorded in `results/cost_summary.json`.

Local machine paths in text records are normalized to relative paths or `<LOCAL>` placeholders. Source-file hashes identify the original artifacts. `checksums.sha256` verifies the distributed bytes.

