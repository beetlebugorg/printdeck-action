# printdeck-action

A GitHub Action that sends the printed parts of a repository to
[Printdeck](https://github.com/beetlebugorg/printdeck), and slices them with the Cura profiles of
the repository.

1. **plan**, at each push, pull request and tag: reads `printdeck.yml`, runs its generate step,
   finds the parts that changed, and sends the plan with a model of each part. The owner opens
   the plan page, chooses the parts (the parts that changed are checked at first), and puts them
   on plates.
2. **slice**, when the owner taps Slice: Printdeck starts the workflow with the ID of the build.
   The Action slices each plate with CuraEngine 5.13 and sends the G-code. The owner checks each
   plate on the build page and approves it. A deck prints a plate only after a person presses
   Start on the deck.

The Action needs no secret. Each request has an OIDC token of GitHub, and Printdeck takes it only
from the repository that the project links. The spec is
[docs/specs/github-builds.md](https://github.com/beetlebugorg/printdeck/blob/main/docs/specs/github-builds.md).

## The workflow

`.github/workflows/printdeck.yml`:

```yaml
name: printdeck
# A push has no title of ours: GitHub then names the run with the first line of the commit.
run-name: >-
  ${{ github.event_name == 'workflow_dispatch'
      && format('Printdeck slice · {0}', inputs.title || inputs.build)
      || github.event_name == 'pull_request'
      && format('Printdeck plan · PR #{0}: {1}', github.event.pull_request.number, github.event.pull_request.title)
      || '' }}
on:
  push:
    branches: [main]
    tags: ["v*"]
  pull_request:
  workflow_dispatch:
    inputs:
      build:
        description: The Printdeck build to slice
        required: true
      title:
        description: The plates of the build, for the title of the run
        required: false

permissions:
  contents: read
  id-token: write   # the OIDC token for Printdeck

jobs:
  plan:
    name: Plan the printed parts
    if: github.event_name != 'workflow_dispatch'
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the base of the pull request, to find the parts that changed
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: beetlebugorg/printdeck-action@v1
        with:
          mode: plan

  slice:
    name: Slice the plates
    if: github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the commit of the plan
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: beetlebugorg/printdeck-action@v1
        with:
          mode: slice
          build: ${{ inputs.build }}
```

A pull request from a fork gets no OIDC token, so it sends no plan.

## printdeck.yml

```yaml
project: bittlex
generate:                       # optional: the step that makes the model files
  install: pip install -r requirements.txt
  run: python bittle_pi_addon/pod.py fast
  check: true                   # fail when the files differ from the committed files
parts:
  - name: Pod body (screw)
    files: [bittle_pi_addon/stl/print/pod_body_screw.3mf]
    printer: taz                # taz (FDM, 280 x 280 mm) or phenom (resin, 276 x 155 mm)
    profile: taz-workhorse-se/pla-0.10-fine   # a folder of profiles/ and a profile in it
    material: roll-4            # a material of the folder
    count: 1                    # copies on one plate
    settings: {support_enable: true, adhesion_type: brim}
  - name: Resin plate
    files: [stl/plates/resin_phenom_plate.stl]
    printer: phenom
    material: Grey resin
    prebuilt: phenom/resin_plate.cbddlp       # a ChiTuBox file in place of a slice
    alone: true                 # never shares a plate
```

- `variants`: the qualities of a part, for example a fine and a standard print. Each variant has
  a `name`, and can change `files`, `profile`, `prebuilt`, `material`, `count`, `alone` and
  `settings` (its settings add to the settings of the part). Each variant is a row of its own on
  the plan page, with its own mark "Changed", so a change of the fine model marks only the fine
  variant. The plates, the G-code and the prints name the variant: "Pod door (fine)".

  ```yaml
  - name: Pod door
    printer: taz
    material: roll-4
    settings: {adhesion_type: brim}
    variants:
      - name: fine
        profile: taz-workhorse-se/pla-0.10-fine
        files: [bittle_pi_addon/stl/print/pod_door_fine.stl]
      - name: standard
        profile: taz-workhorse-se/pla-0.20-standard
        files: [bittle_pi_addon/stl/print/pod_door.stl]
  ```
- `files`: STL or 3MF. A 3MF file keeps the Cura settings of its objects, for example a support
  blocker (`cura:anti_overhang_mesh`).
- `settings`: Cura settings of the part. A setting that Cura allows for each mesh (for example
  `support_enable`) stays with its part on a shared plate. Another setting (for example
  `adhesion_type` or `brim_line_count`) is for the whole plate: two parts with other values of
  it cannot share a plate, and a value of one part applies to the whole plate.
- `generate.check`: the dates in 3MF and STEP files do not count.

## The profiles

`profiles/<folder>/` is a Cura stack, made by `slicer/export_cura_profile.py` of Printdeck from the
Cura app (see [profiles/README.md](https://github.com/beetlebugorg/printdeck/blob/main/profiles/README.md)).
`curaslice.py` works out each setting as the Cura app 5.13 does. A slice of a part matches the
slice of the Cura app with the same profile: the same time, filament, size and layers. Each
G-code has the profile and its version (`;Printdeck profile: taz-workhorse-se/pla-0.10-fine@44606b4e58b6`),
and the build, the commit and the title.

## CuraEngine

`curaengine.sh` takes CuraEngine 5.13.0 and its libraries from the official AppImage of Cura
5.13.0, after a check of its SHA-256. The Action keeps them in the cache of GitHub Actions, so
only the first slice run downloads the AppImage.

## Tests

```
python3 -m unittest test_curaslice test_action
CURAENGINE=/path/to/CuraEngine CURAENGINE_LIB=/path/to/libs python3 -m unittest test_curaslice   # with a real slice
```

## License

AGPL-3.0, as Printdeck. CuraEngine (AGPL-3.0) and the Cura definitions (LGPL-3.0) come from
UltiMaker, unchanged.
