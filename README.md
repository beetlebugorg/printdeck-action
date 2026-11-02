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
      || '' }}
on:
  # Each push of a branch or a tag sends a plan. Printdeck makes the plan of a branch with an open
  # pull request the plan of that pull request, also for a branch of a member's fork.
  push:
    branches: ["**"]
    tags: ["v*"]
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
    if: github.event_name == 'push'
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # the default branch, to find the parts that changed on a branch
          persist-credentials: false   # the code of a pull request reads no token in .git/config
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
          # The commit alone, with no history: the Action fetches the commit of the plan, and the
          # checkout gets only the files of that commit.
          fetch-depth: 1
          filter: blob:none
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: beetlebugorg/printdeck-action@v1
        with:
          mode: slice
          build: ${{ inputs.build }}
```

### Pull requests of members' forks

A member of the project can fork the repository, push a branch to the fork, and open a pull
request. The fork has the same workflow, so the push runs the plan job in the fork, with the
OIDC token of the fork. The repository of the project runs nothing for a fork.

Printdeck takes the plan of a fork's token only when all of these are true:

1. GitHub signed the token, for the audience of Printdeck, and it is not used already.
2. The repository of the token is a fork of the repository of a project.
3. The owner of the fork is a member of that project, with that GitHub username on the project
   page. The owner of the project sets the usernames.
4. An open pull request of the fork into the repository of the project has the commit of the
   token as its head.

The plan is then the plan of that pull request, with the name of the member. A pull request of a
stranger, or a push to a fork with no pull request, makes no plan. A fork cannot slice: only
`printdeck.yml` on the default branch of the project's repository fills a build, and it reads
the commit of the pull request as data.

The member turns on the Actions of the fork once (GitHub turns them off in a new fork). The
GitHub App of Printdeck needs the permission Pull requests: Read, to find the pull request.

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
docs:                           # optional: the PDFs of the project, for the project page
  - name: Build guide
    file: bittle_pi_addon/bittle_pi_addon_guide.pdf
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
- `docs`: at most 10 PDFs, each at most 50 MB, after the generate step. The plan run uploads
  each PDF with a picture of its first page (`pdftoppm` of poppler-utils; the run installs it on
  GitHub when the runner has none). The project page shows the PDFs of the newest plan of the
  default branch, else of the newest plan of a tag, with a link to the file on GitHub.

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
