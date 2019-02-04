## Developing QSP-Protocol-Node
1. Checkout a branch off `develop`

1. Make changes

1. Run `make test`. Fix any issues or update the tests if applicable

1. [Run the node](#run-locally) and check if there are any issues

1. Open a pull request from your branch into `develop`

1. Wait for CI tests to finish and pass

1. Update changelog file for related release


### Creating a Betanet distribution bundle

Currently, the process is mostly manual. To be automated in the future.

### Release
1. Run `make bundle`. If successful, this  will create `qsp-protocol-v1.zip` file under deployment/local.
1. Upload the file to Google Drive (`QSP Protocol V1 - Release Bundles`)
1. Using Google Drive sharing features, share the file with a whitelisted node operator

### Development hierarchy

* Main file: `qsp_protocol_node/__main__.py`

* Target environments are defined in `deployment/local/config.yaml`

* QSPAuditNode gets params from YAML
  - `config.py`
    - provides an interface for accessing configured components
    instantiated from the settings in the YAML file
  - `audit.py`
    - main loop is in the run() method
    - logic for audit computation
    - report is JSON and posted to Ethereum
  - `analyzer.py`
    - abstracts an analyzer tool
    - needs to accept parameters in a better way

### Codestyle

The codestyle builds on PEP8 and includes especially the following:

1. Indentation is done using spaces in multiples of 4
2. Lines are broken after 100 characters, longer lines are allowed in exceptional cases only
3. Methods are separated with 2 blank lines
4. Do not use parentheses when not necessary
5. `import` statements come before `from import` statements
6. Import only one module per line
7. Remove unused imports
8. Use lowercase_underscore naming for variables
9. Use `is` and `is not` when comparing to `None`
10. Beware of overriding built-ins
