# Thingiverse car-parts savings

This is a rebuild of the lost code behind the study of what consumers save by 3-D printing car parts from Thingiverse instead of buying them, and behind its review site, savings-review.vercel.app. It follows the paper draft (3dp_repair_v1jmp), the methods write-up (Methods - Printable Car Parts Savings), the pages that were deployed on Vercel, and the Thingiverse email thread.

The offline half is checked against the 2 September 2026 run. From the Sep 2 rows, the site builder with `--as-deployed` reproduces all 80 deployed pages byte for byte; without it, the builder corrects the places where the deployed pages contradict the paper (listed under "Corrections to the deployed pages"). Step 5 re-derives every stored figure on all 9,190 rows. Romain's sheet produces the same 9,190 Things with the same download counts and vehicles.

The live half is written to the paper, and every part of it has run live. On 12 October 2026 the small test below ran end to end with browser cookies and an OpenAI key: Thingiverse pages and files, eBay searches, GPT-5.6 Luna for the three AI steps, and real slices in OrcaSlicer 2.4.2 through orcaslicer-mcp 0.1.16. The 17-Thing sample ran live the same day. The DuckDuckGo fallback and the slicer's Remote API have also run live. The three prompts were lost with the code and are rewritten from the paper's descriptions, so expect classifications and listings to differ somewhat from Sep 2. Start with the small test below.

## How the paper maps to the code

| Paper | Code | What it does |
|---|---|---|
| Data collection | `carsavings/thingiverse.py` | Reads each Thing from thingiverse.com with a signed-in browser's cookies, through the JSON route the site's own pages call: name, creator, description, photos, model files (STL, 3MF, OBJ) and recommended infill. Download counts come from the input sheet, or from a signed-in token when the sheet has none. When the JSON route refuses, it falls back to the page's og tags. |
| Steps 1 and 2 | `carsavings/classify.py` | One GPT-5.6 Luna call on the text and photos: spare part, accessory or toy/decor; the "not sold on its own" flag; the part's name; the pricing tier; the search query; and which files make up one printable unit. |
| Step 3, retrieval | `carsavings/ebay.py`, `carsavings/websearch.py` | The top 10 active eBay listings in Best Match order, renumbered cheapest first. DuckDuckGo is the fallback, used only when eBay has no listing at all. |
| Step 3, verification | `carsavings/verify.py` | The matcher (same physical part for the same vehicle, from both sides' photos), then the judge (is the price plausible for that part). Both reasons stay on the row. |
| Step 4 | `carsavings/slicing.py` | Slices the Thing's own files in OrcaSlicer 2.4.2 through the orcaslicer-mcp connector, at the infill from the Thing's page. Grams are priced at $13.99/kg of PLA (`PLA_USD_PER_KG`). |
| Step 5 | `carsavings/savings.py` | The checks (scale guard at $100 per gram, bed size 256 mm, judge refusals, multi-unit flag), then saving per print = round(max(price − print cost, 0), 2), total = round(saving × downloads, 2), and the bucket: repair, assembly-only or upgrade. Every counted part lands in exactly one bucket, so the three totals can be added without double counting. |
| Review page | `carsavings/site.py`, `carsavings/templates/`, `site/api/` | The overview, the all-rows list and the 78 brand pages, plus the decision store and image proxy that ran on Vercel. |
| Review merge | `script/apply_review.py` | Folds reviewers' decisions back into a new table; the original is never overwritten. |

`pipeline.py` runs the steps in order (`carsavings/run.py`). Every stage's result is cached under `cache/`, so a stopped run picks up where it left off.

## Setup

You need Python 3.10 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`requirements.txt` includes curl_cffi, which sends requests with Chrome's TLS handshake. Cloudflare and eBay challenge Python's own HTTP client even when it carries valid cookies, so the live run needs it.

Fill in `.env`. Each line of `.env.example` says where its value comes from. For a first test you need:

- `OPENAI_API_KEY`.
- `THINGIVERSE_COOKIE`: in Chrome, signed in to thingiverse.com, open a Thing, then DevTools → Network → reload → the first request → Request Headers → copy the whole `cookie` value. It must contain `cf_clearance`.
- `EBAY_COOKIE`: on ebay.com or ebay.ca, run any search and copy that page's `cookie` header the same way. Every search URL asks eBay to ship to New York (`EBAY_EXTRA_PARAMS`; leave it empty for that default), so prices come back in US dollars from Canada too, without changing your ship-to address.
- `BROWSER_USER_AGENT`: the `user-agent` header from the same browser. Cloudflare ties the cookie to it.

`THINGIVERSE_WEB_TOKEN` can stay empty. The harvest reads each Thing with the visitor token that Thingiverse gives every browser, which is enough for the full description, the photo gallery, the print settings and the model files. A signed-in token only adds download counts for an input sheet without a Downloads column; Romain's sheet, the Prius sheet and the examples all have one.

The cookies expire. When the doctor or a run reports a Cloudflare or eBay challenge, copy them from the browser again.

Then check everything:

```sh
python script/doctor.py --skip-slicer
```

The doctor prints PASS, WARN or FAIL for each setting with what to fix, and never prints a secret.

### OrcaSlicer (step 4)

Skip this section for a first test, because `--skip-slice` prices every print at the flat $0.42 default. To slice:

1. Install the OrcaSlicer MCP build (OrcaSlicer 2.4.2 with the Remote API) from github.com/MaxEllis/OrcaSlicer/releases; on a Mac that is `OrcaSlicer-MCP-2.4.2-mcp.12-macos-universal.dmg` or later. The build is not notarized, so if macOS blocks its first launch (it does when a browser downloaded the file), open System Settings → Privacy & Security and click Open Anyway.
2. On first launch, pick the printer, process and filament you slice with. The connector slices with whatever presets are active, so the grams depend on them; the pipeline sets only the infill, per Thing. The paper names no profile. This rebuild was tested with Bambu Lab X1 Carbon 0.4 nozzle, `0.20mm Standard @BBL X1C` (15% infill) and `Bambu PLA Basic @BBL X1C`: the X1 Carbon's own defaults, on the 256 mm bed that `BED_MM` assumes. The filament profile's own price is not used: the pipeline prices the grams at `PLA_USD_PER_KG`.
3. In Preferences → Remote API, tick Enable, copy the Token into `ORCA_API_TOKEN`, and close Preferences; the server starts when Preferences closes. Leave Bind LAN off and the port at 13130, which is what `ORCA_API_URL` expects.
4. Install uv, which provides `uvx`. The pipeline starts `uvx orcaslicer-mcp` itself. The slicer code was checked against orcaslicer-mcp 0.1.16: every tool it calls exists there with the same arguments and result fields.
5. Run `python script/doctor.py` without `--skip-slicer`.

Keep OrcaSlicer open while the pipeline runs. It clears the plate before each file and puts your own infill back at the end, so save any project you have open first. Slices are cached by file and infill, not by preset, so after switching the printer, process or filament preset, rerun with `--refresh slice`.

`SLICER_BACKEND=rest` calls the Remote API directly, without the connector. `SLICER_BACKEND=none` estimates grams from each file's geometry, without a slicer.

## A small test

`examples/smoke.csv` holds five Things, one of each main outcome:

```sh
python pipeline.py --csv examples/smoke.csv --workers 2 --skip-slice
python script/compare_run.py
python script/build_dashboard.py --csv out/cars_final.csv --out site/cars
```

With OrcaSlicer set up as above, leave out `--skip-slice` and each Thing's own files are sliced, so the print costs come from the slicer instead of the flat $0.42.

On 12 October 2026 the test ran live and sliced, with the X1 Carbon presets named in the OrcaSlicer section and PLA at Sep 2's $25/kg:

| Thing | Sep 2 result | 12 Oct 2026 |
|---|---|---|
| 3013735, Mustang rear cigarette tray | repair: $19.99 eBay listing, print cost $0.431, 309 downloads, $6,044.04 | assembly-only: $18.74 for a used 1987–1993 Mustang console ashtray, print cost $0.416, $5,660.88 |
| 5998784, Saab 900 headlight wire cap | assembly-only: $19.99, print cost $0.092, 98 downloads, $1,950.20 | assembly-only: $22.99, $0.091, $2,244.20 |
| 7186295, Kia EV6 knob extensions | upgrade: $17.09, print cost $0.057, 23 downloads, $391.69 | upgrade: $16.14, $0.053, $370.07 |
| 5019865, Mazda 121 sun visor clip | not counted: no listing passed the matcher; 10 candidates kept for review | not counted, for the same reason |
| 5770631, Jeep Christmas ornaments | toy/decor: never priced | toy/decor: never priced |

With `--skip-slice`, every print costs the flat $0.42, so Sep 2's three listings would give $6,047.13, $1,917.86 and $383.41.

`compare_run.py` prints each field next to its Sep 2 value, with Sep 2's print cost and total re-priced at this run's PLA price. The class, the standalone flag, the tier and the bucket should mostly agree. On 12 October the class and the price source agreed for all five Things, and the standalone flag, the tier and the bucket for four. The Mustang moved to assembly-only because the classifier marked its tray as not sold on its own, and the matcher then accepted the whole console ashtray. Queries, listings and prices will drift, because eBay changes daily and the model is not deterministic. Print costs differ slightly too; see "Print profile" below.

The pages in `site/cars/` open from disk with every number, but photos load through the deployment's `/api/img`, so they appear only once `site/` is deployed.

Next steps up:

- `--csv examples/sample_things.csv` runs 17 Things that cover every Sep 2 outcome: the scale guard, an oversize part, a judge refusal, a deleted Thing, the DuckDuckGo fallback, the flat default print cost and others. Run it with OrcaSlicer to test step 4. `examples/expected_sep2.json` holds the Sep 2 values that `compare_run.py` uses. On 12 October 2026 it ran live and sliced at $25/kg, with no model or other errors: six Things counted (assembly-only $7,905.08 from two parts, upgrade $127,888.63 from four), the deleted Thing was marked as deleted, and the bed-size guard stopped the Audi A2 cup holder (272 mm, as in Sep 2) and the Lotus Elise front splitter (1,400 mm; see "Rows at the flat default cost" below). The rest of the differences came from the model and from eBay. The MX-5 fender vent matched a $59.99 pair where Sep 2 had $150, and the Rivian tray, whose $13.49 Tesla Model Y listing Sep 2's judge refused, matched a $57.44 Rivian console tray. Three Things that Sep 2 matched found no accepted listing this time: the Ferrari carburetor spacer, which Sep 2 priced at $33 through DuckDuckGo (eBay returned ten listings this time, so the fallback did not run); the Alfa Romeo phone holder, $19.99 in Sep 2; and the NSX cup holder, which Sep 2 matched at $129.80 and then held out with the scale guard. Four changed class: the Toyota C-HR "mug", a divided insert that sits in a cup holder, went from toy/decor to accessory; the NSX wheels, which Sep 2 sliced at 0.60 g, went from spare part to toy/decor; and the NSX and Audi A2 cup holders went from spare parts to accessories. The Jeep bracket, whose photo made Sep 2's model call fail, was classified this time.
- `--csv data/inputs/toyota_prius_parts.csv --limit 5` runs a few Things from the Toyota Prius sheet.
- `--csv data/inputs/validated_matches_2026-08-13.csv --site` is the full Sep 2 corpus (9,190 Things). It takes days at the polite request rate. Stop it at any time; rerunning continues from the cache.

## Running it

```sh
python pipeline.py --csv <sheet> [--limit N] [--workers 4] [--skip-slice] [--site]
python pipeline.py --ids 3013735 https://www.thingiverse.com/thing:5998784
```

The input can be Romain's validated-matches export, the Prius sheet, any CSV with an id or URL column, a text file of ids, or a previous output table (gzipped or not). Output goes to `out/cars_final.csv`, one row per Thing in input order. The columns are listed in `carsavings/schema.py`. Rows that are not counted stay in the table at $0, with the reason in `err` or in the judge's text.

`--refresh <stage>` redoes one stage (harvest, classify, search, verify or slice) and every stage after it. A failed harvest, a failed model call or an unreachable DuckDuckGo is retried automatically on the next run.

The run stops rather than writing bad rows when Thingiverse or eBay challenges the cookies, when eBay prices in another currency, when OpenAI refuses the key or the model, or when the slicer cannot be reached. Fix the setting and rerun. A failure that belongs to one Thing, such as a deleted Thing or a model call that failed four times, is written on that row and counts $0.

## Review

1. Build the pages: `python script/build_dashboard.py --csv out/cars_final.csv --out site/cars`.
2. For an older table whose uncounted rows have no candidate pool, `python script/capture_candidates.py --csv out/cars_final.csv` adds pools (add `--refresh` for searches that came back empty on the day). Then build from `out/cars_final_pools.csv`.
3. Deploy `site/` to Vercel and connect a Blob store (Storage → Create Blob → Connect Project) so all reviewers share decisions, one file per brand. Without Blob, decisions stay in each reviewer's browser, and each brand page can export them as JSON.
4. Merge the decisions with `python script/apply_review.py --csv out/cars_final.csv --url https://<deployment>`, or with `--decisions <export.json>`. This writes `out/cars_final_reviewed.csv` with four `human_review` columns. Rebuild the pages from that file.

A veto removes a counted row's dollars. Accepting a listing re-prices a counted row, or rescues an uncounted one into its bucket. Keep confirms a row. Shipping is recorded and never added to the price. A decision that carries no photo keeps the photo already on file for that listing, the row's own or the matching candidate's.

For the paper, one of the authors made the review decisions by hand through the review page; the `human_review` columns hold those decisions. The review rejected 8 counted rows, and 3 more were removed because their prices broke the pricing rules, so the paper counts 4,317 parts where Sep 2 counted 4,328. In the paper's reviewed table, the same columns also mark the 421 multi-unit rows that a script divided by their unit count (as `swapped`, dated 2026-10-13) and those 3 removals (as `vetoed`).

## Rebuilding the Sep 2 site

```sh
python script/build_dashboard.py --csv data/sep2/cars9k_final.csv.gz --out site/cars \
    --generated 2026-09-02 --source cars9k_final.csv --pool-prose-count 247 --as-deployed
```

This writes the 80 deployed pages byte for byte (index.html, all.html and the 78 brand pages; their SHA-256 hashes are in `data/sep2/deployed_sha256.json`), plus the 1,451 candidate pools. Leave out `--as-deployed` to build the same data with the corrections below. `data/sep2/cars9k_final.csv.gz` holds the 9,190 rows embedded in the deployed brand pages, put back in the order of Romain's sheet. The pages did not embed the working columns (grams, print hours, size, infill, files and the download-count source), so those columns are empty in this file. `script/compare_sep2.py` runs the same check from the raw deployed pages, which live in the methods-doc folder and are not in this repo.

## Corrections to the deployed pages

The deployed pages disagree with the paper in a few places. The builder fixes them by default, and `--as-deployed` keeps the deployed versions. `CORRECTIONS` in `carsavings/site.py` holds each original passage next to its replacement; the median is worked out in `full_meta` and the review-state keys are set by `STATE_KEY`, in the same file.

- **Assembly-only.** The overview called it "a subset of repair, already included in the repair figure". Every counted part sits in exactly one bucket, as the paper says, so the overview now says so, keeps assembly-only out of the repair figure and gives the combined total of the three buckets.
- **The funnel.** The deployed funnel subtracted the 24 scale-guard rows twice, so its "Printable at that scale" step read 4,488, 24 too low, and its third step was labelled "eBay returned listings" although it counted every searched row. The corrected funnel reads 9,190 Things, 8,985 priceable, 8,962 searched, 4,552 matched to the same physical part and 4,328 counted. Those are Sep 2's rows before review; the paper counts 4,317 after review (see "Review" above).
- **Combined totals.** Every counted part belongs to exactly one bucket, so the three totals can be added without double counting; the paper adds them to $28,956,030 for 4,317 parts. The overview said the buckets were never summed and now prints their combined total, and all.html keeps its total of all three. The review report's headline and its `combined_total` added repair and upgrade but left out assembly-only, so the headline now gives repair after review and the partial sum is gone. The brand pages still leave out the combined row and card that the Sep 2 pages had.
- **Median saving per print.** The overview took the median only over counted rows that saved something and printed it in whole dollars. It now takes the median over every counted part, as the paper does, and prints it to the cent: $12.93 for the paper's 4,317 parts.
- **Listing sources.** The overview said every counted row was priced against a real eBay listing, but a few were priced through the DuckDuckGo fallback or from a listing typed in during review. It now gives the share priced on eBay, which is 99.4% of the paper's 4,317 parts.
- **Rounding.** The deployed pages re-priced a swapped listing with JavaScript's `Math.round`, which lands a cent high on 173 of the 4,328 counted rows. Swapping all 485 counted Toyota rows to their own listings moved that page's totals by $76, and all.html showed $147.67 per print beside a total worked out from $147.66. The pages now round the way the pipeline does, so a swap at a row's own price changes nothing, and they agree with the pipeline on all 4,328 counted Sep 2 rows. The paper reports the same agreement on its 4,317 counted parts.
- **Listings without a saved photo.** 184 of the paper's 4,317 counted parts have a listing link but no saved photo: 152 listings that reviewers entered from outside the saved pool, 28 rows priced through DuckDuckGo (one of them divided per unit by the script) and 4 eBay rows that had none in the run. (There were 738 until `apply_review.py` kept the photo on file when a decision carried none.) The overview showed such a row as a broken image (5 of the 40 rows worth checking first, on the paper's data) and all.html as "no listing". Both now show a "no photo" tile, and all.html's detail pane says "no listing photo saved".
- **Wording.** The pages described a with-shipping sensitivity that the dashboard never had, said the listings were eBay's sold listings when the pipeline searches active ones, and named a `--all` flag that `capture_candidates.py` does not have.
- **Review-state keys.** The pages keep review decisions in one Vercel Blob per brand and in each reviewer's browser. The default build uses fresh keys (`r2-<slug>` for the Blob, and `tv-review-decisions-r2-<slug>` and `tv-all-decisions-r2` in the browser), so its pages start without the decisions saved on pages built before; `--as-deployed` keeps the original keys. `script/apply_review.py --url` still reads the original keys, so pass decisions saved under the new ones with `--decisions`, as a brand page's Export JSON or a saved `/api/state?key=r2-<slug>` answer.

## Tests

```sh
python -m unittest discover -s tests -v
```

The 92 tests run offline in about three seconds. They cover the byte-identical Sep 2 rebuild and every stored Sep 2 figure; the corrected pages, including a check that runs the pages' own JavaScript in Node and re-prices all 4,328 counted Sep 2 rows to the pipeline's cent; step 5's arithmetic; the eBay, DuckDuckGo and Thingiverse parsers on synthetic pages; the Thingiverse session on a fake site (the visitor token, replacing a refused or expiring one, and the fallback to page tags); the whole pipeline on fakes (every kind of row, resuming from the cache, `--refresh` and a blocked session); slicing, with replies shaped like OrcaSlicer's own, and geometry; and the review merge and helper scripts. Tests that need a package or Node that you have not installed skip themselves. With ruff installed (`pip install ruff`), `ruff check .` applies the lint rules in `ruff.toml`.

## Known differences from the Sep 2 run

- **PLA price.** Sep 2 priced plastic at $25/kg. The paper now uses $13.99/kg, the Amazon.com price of a 1 kg SUNLU PLA spool on 13 October 2026, so that is the default, and the flat 30 g default costs $0.42 instead of $0.75. The paper's figures start from Sep 2's rows with every print cost re-priced at $13.99/kg. `PLA_USD_PER_KG=25` gives Sep 2's print costs. The Sep 2 rebuild and its tests read Sep 2's stored print costs, so the price setting does not touch them.
- **No price estimates.** Sep 2 counted two rows priced by a model estimate (6617133 and 5831802). The paper says parts without an accepted listing stay unknown at $0, so this code never estimates a price.
- **Deleted or private Things.** Thingiverse serves its default page tags for these. Sep 2 marked 40 as errors but sent 16 more to the classifier, which called 13 of them toys and 3 accessories. This code marks all of them as errors at $0.
- **Photos.** Every photo is converted to JPEG before it goes to the model. Sep 2's one model failure (6948073) was OpenAI rejecting an image format. Gallery photos are fetched through Thingiverse's own image resizer at up to 1,024 pixels a side, the largest size the model is sent anyway.
- **Vehicle years on the sheet.** Romain's sheet attributes each Thing's vehicle automatically, and the generation and years can be wrong: it lists 2023–present for a tray made for 1987–1993 Mustangs, and 1994–1998 for a 1979–1986 Saab 900 cap. The matcher and the judge are told to follow the Thing's own title, description and photos when they disagree with the sheet.
- **Rounding.** The pipeline uses Python's round(), as Sep 2 did, which reproduces all 4,328 counted rows. The review pages built here round the same way; the deployed ones did not (see "Corrections to the deployed pages").
- **Unmatched rows.** Every row without an accepted listing gets the fixed judge text "no strictly-verified listing — row not counted". Sep 2 had that text on only 202 rows.
- **Candidate pools.** The pool is stored on every row searched on eBay, so reviewers can rescue any unmatched row. Sep 2 had pools on 1,451 rows.
- **DuckDuckGo** runs only when eBay returns nothing. If DuckDuckGo is unreachable, the row stays unmatched and is searched again on the next run. The query is posted through the system's curl, because DuckDuckGo answers the plain GET URL, and most requests from Python's own HTTP client, with a bot check (HTTP 202).
- **Slicing.** Each file of the printable unit is sliced on its own plate and the grams are added together. When the classifier picks a 3MF or OBJ file and an STL of the same name sits beside it, the STL is sliced, because the paper slices the Things' STL files; the Kia knob's 3MF is a plate of two knobs, which would double its print cost. Other 3MF files are loaded as plain meshes, so their embedded settings are ignored. A geometry estimate of 0 g or less falls back to the 30 g default.
- **Infill.** The infill comes from the Thing's print settings, or from its write-up: "Infill: 20%", "15% infill", or a heading with the value on the next line. A range is read at its top, so "15-20%" gives 20%, the heavier print and so the smaller saving. In the write-up the value needs a % sign and must come within a few words of "Infill" without crossing a line break, a comma, a full stop or another number, so a percentage that belongs to a neighbouring setting is never taken, and neither is "Infill: approx. 20%". Without a value, the slicer keeps the active process preset's infill (15% for the X1 Carbon preset).
- **Rows at the flat default cost.** A row priced at the default 30 g has no grams and no size, so neither the scale guard nor the bed-size guard can apply to it. Sep 2 counted 5180561, a Lotus Elise S2 front splitter matched to a $1,043.28 carbon-fibre splitter, that way, for $286,695.75, about 1.8% of Sep 2's repair total. This code downloads the Thing's STL, measures it at 1,400 mm and stops it at the bed-size guard. On 12 October the same listing was matched again, and the guard left the row out. The review behind the paper's figures rejected the row too.
- **Print profile.** Sep 2's slicer profile was not recorded. Nine Things in the 17-Thing sample were sliced in OrcaSlicer in both runs. With the X1 Carbon presets their grams came to 2.3% less than Sep 2's in total. One at a time they ranged from 15% below Sep 2's to 3% above, five of them within 3.5%. The exception is the NSX cup holder, a fraction of a gram either way (0.20 g in Sep 2, 0.35 g now). No single infill closes the gap: at 15% the Saab cap is within 0.5% of Sep 2, the Mazda 121 clip (15% lower) would need about 30%, and the Kia knob barely changes with infill. So Sep 2's profile differed in other settings, such as walls or line widths. Selecting the printer and process presets of the machine that ran Sep 2 should bring the grams closer.
- **No temperature** is sent to the model. The paper gives no decoding settings.
- **Pool count in the prose.** all.html's "has only reached N of the M uncounted rows" is computed from the table. The deployed page said 247, a stale number; `--pool-prose-count` pins it.
- **Moving a row to another bucket.** The paper lists this as a review action, but the deployed pages had no control for it. `apply_review.py` reads an optional `b` field (repair, assembly or upgrade) on a decision; add it by editing the exported JSON.

## Layout

```
pipeline.py              the run (python pipeline.py --help)
carsavings/              the pipeline, step 5 and the site builder
  templates/             the deployed pages with their data taken out
script/
  doctor.py              checks every setting before a run
  harvest_things.py      harvest only: test the Thingiverse session, warm the cache
  compare_run.py         a run next to the Sep 2 values
  build_dashboard.py     the review pages from a table
  capture_candidates.py  candidate pools for uncounted rows
  apply_review.py        reviewers' decisions back into a new table
  compare_sep2.py        rebuild check against the raw deployed pages
site/                    the Vercel project: api/state.js (decisions in Blob), api/img.js (image proxy)
data/inputs/             Romain's validated-matches sheet (9,190 Things) and the Toyota Prius sheet (60)
data/sep2/               the Sep 2 rows and the deployed pages' hashes
examples/                smoke.csv (5 Things), sample_things.csv (17), expected_sep2.json
tests/                   python -m unittest discover -s tests
```

Secrets live only in `.env`, which git ignores. Caches, downloaded model files, output tables and built pages are ignored too.
