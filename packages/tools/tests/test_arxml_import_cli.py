"""Tests for ``arxml_import`` and ``swcraft import model``: YAML emission, ImportReport, the CLI.

Fixture-driven tests run the whole pipeline against `composition_nested.arxml`
(`SensorSwc` instantiated twice inside `InnerComposition`, `ActuatorSwc` once): a
prototype is a composition **member**, and no file is named after one.
`unsupported_content.arxml` is the "skips alone" fixture: real content plus several
skip-worthy elements. Exports the shared fixtures cannot carry without perturbing
other tests are built inline in `tmp_path`.

The import writes no requirement and no ``satisfies``, and an imported component
without a link is no finding.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest
import yaml
from conftest import declare_no_support, layer_directories, model_element_path, relabel_arxml_version
from element_identity_support import declared_tree
from swcraft_tools.arxml_import import run_import
from swcraft_tools.arxml_import.report import (
    NO_REQUIREMENT_SENTENCE,
    ImportReport,
    file_suffix_heading,
    imported_file_path,
)
from swcraft_tools.arxml_import.unresolved import CAUSE_CONFLICT, CAUSE_SELECTION
from swcraft_tools.arxml_reader import SkipCategory, SkipEntry, StandardRule, read_arxml
from swcraft_tools.arxml_version import ArxmlVersion
from swcraft_tools.cli import main
from swcraft_tools.cli.init_command import _default_file_plan
from swcraft_tools.identifier import MAX_IDENTIFIER_LENGTH, REASON_LENGTH, check_identifier
from swcraft_tools.reader import read_data_types, read_interfaces, read_layer, read_requirements, read_swc_model
from swcraft_tools.validator import check_cross_layer_consistency, check_unknown_keys
from swcraft_tools.writer import add_port, create_data_type, create_interface, create_swc, generate_arxml
from swcraft_tools.writer.arxml_generation import INTERNAL_BEHAVIOR_SUFFIX

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "arxml"
COMPOSITION_NESTED = FIXTURES_DIR / "composition_nested.arxml"

#: A component name ``reserved_swc_names`` refuses on the composition fixture: it
#: collides with the ``SWC-IMPLEMENTATION`` generated for ``SensorSwc``.
_RESERVED_COMPONENT = "SensorSwc_Implementation"

#: The fixture with that collision substituted in; ``ActuatorSwc`` is instantiated
#: once, so the collision is the only thing that moves.
_RESERVED_COMPONENT_EXPORT = COMPOSITION_NESTED.read_text(encoding="utf-8").replace("ActuatorSwc", _RESERVED_COMPONENT)
UNSUPPORTED_CONTENT = FIXTURES_DIR / "unsupported_content.arxml"
INIT_VALUES = FIXTURES_DIR / "init_values.arxml"

#: ``GroupB``'s base type, the anchor an insertion goes in front of.
_GROUP_B_BASE_TYPE = "                <SW-BASE-TYPE>\n                  <SHORT-NAME>uint8B</SHORT-NAME>"

#: ``GroupA``'s base type, the anchor an insertion goes in front of.
_GROUP_A_BASE_TYPE = "                <SW-BASE-TYPE>\n                  <SHORT-NAME>uint8A</SHORT-NAME>"

#: An application primitive ``My Type`` in ``GroupA``'s package.
_APP_PRIMITIVE_OF_THE_CONTESTED_NAME = (
    """\
                <APPLICATION-PRIMITIVE-DATA-TYPE>
                  <SHORT-NAME>My Type</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                </APPLICATION-PRIMITIVE-DATA-TYPE>
"""
    + _GROUP_A_BASE_TYPE
)

#: An application record of the same bare name in ``GroupB``'s package, typed by
#: that primitive: the one-layer bare-name collision ``_reserve()`` renames.
_APP_RECORD_OF_THE_CONTESTED_NAME = (
    """\
                <APPLICATION-RECORD-DATA-TYPE>
                  <SHORT-NAME>My Type</SHORT-NAME>
                  <CATEGORY>STRUCTURE</CATEGORY>
                  <ELEMENTS>
                    <APPLICATION-RECORD-ELEMENT>
                      <SHORT-NAME>val</SHORT-NAME>
                      <CATEGORY>VALUE</CATEGORY>
                      <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/GroupA/DataTypes/My Type</TYPE-TREF>
                    </APPLICATION-RECORD-ELEMENT>
                  </ELEMENTS>
                </APPLICATION-RECORD-DATA-TYPE>
"""
    + _GROUP_B_BASE_TYPE
)


TWO_GROUPS_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>GroupA</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>DataTypes</SHORT-NAME>
              <ELEMENTS>
                <SW-BASE-TYPE>
                  <SHORT-NAME>uint8A</SHORT-NAME>
                  <CATEGORY>FIXED_LENGTH</CATEGORY>
                  <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
                  <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
                  <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
                </SW-BASE-TYPE>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>uint8A_impl</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/GroupA/DataTypes/uint8A</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Interfaces</SHORT-NAME>
              <ELEMENTS>
                <SENDER-RECEIVER-INTERFACE>
                  <SHORT-NAME>IGroupA</SHORT-NAME>
                  <IS-SERVICE>false</IS-SERVICE>
                  <DATA-ELEMENTS>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>val</SHORT-NAME>
                      <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/GroupA/DataTypes/uint8A_impl</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                  </DATA-ELEMENTS>
                </SENDER-RECEIVER-INTERFACE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>SwcA</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pOut</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/GroupA/Interfaces/IGroupA</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>SwcA_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>GroupB</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>DataTypes</SHORT-NAME>
              <ELEMENTS>
                <SW-BASE-TYPE>
                  <SHORT-NAME>uint8B</SHORT-NAME>
                  <CATEGORY>FIXED_LENGTH</CATEGORY>
                  <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
                  <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
                  <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
                </SW-BASE-TYPE>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>uint8B_impl</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/GroupB/DataTypes/uint8B</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Interfaces</SHORT-NAME>
              <ELEMENTS>
                <SENDER-RECEIVER-INTERFACE>
                  <SHORT-NAME>IGroupB</SHORT-NAME>
                  <IS-SERVICE>false</IS-SERVICE>
                  <DATA-ELEMENTS>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>val</SHORT-NAME>
                      <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/GroupB/DataTypes/uint8B_impl</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                  </DATA-ELEMENTS>
                </SENDER-RECEIVER-INTERFACE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>SwcB</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pOut</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/GroupB/Interfaces/IGroupB</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>SwcB_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

TRAVERSAL_GROUP_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>..</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>EvilSwc</SHORT-NAME>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>EvilSwc_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Components</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>GoodSwc</SHORT-NAME>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>GoodSwc_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


PROTOTYPE_NAME_TRAVERSAL_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Components</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>SensorSwc</SHORT-NAME>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>SensorSwc_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Compositions</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>OuterComposition</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>SensorGood</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Components/SensorSwc</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>../Evil</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Components/SensorSwc</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


#: Two composition packages plus a root; one places a prototype of a type this
#: export does not carry and wires a connector onto it, so a ``--packages``
#: selection can drop the stub's composition. The connector is kept (its end names
#: a port under the stub's own ``TYPE-TREF``).
ONTO_STUB_PACKAGES_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Shared</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>uint8</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Sig_impl</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Shared/uint8</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ISig</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>val</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Shared/Sig_impl</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>ComponentsA</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>SwcA</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pOut</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Shared/ISig</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>SwcA_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>ComponentsB</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>SwcB</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pOut</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Shared/ISig</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>SwcB_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>CompositionsA</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>CompA</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>SwcA_1</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/ComponentsA/SwcA</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>Ghost_1</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Elsewhere/Ghost</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
              <CONNECTORS>
                <ASSEMBLY-SW-CONNECTOR>
                  <SHORT-NAME>Assembly_A_to_Ghost</SHORT-NAME>
                  <PROVIDER-IREF>
                    <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">/Acme/CompositionsA/CompA/SwcA_1</CONTEXT-COMPONENT-REF>
                    <TARGET-P-PORT-REF DEST="P-PORT-PROTOTYPE">/Acme/ComponentsA/SwcA/pOut</TARGET-P-PORT-REF>
                  </PROVIDER-IREF>
                  <REQUESTER-IREF>
                    <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">/Acme/CompositionsA/CompA/Ghost_1</CONTEXT-COMPONENT-REF>
                    <TARGET-R-PORT-REF DEST="R-PORT-PROTOTYPE">/Elsewhere/Ghost/rIn</TARGET-R-PORT-REF>
                  </REQUESTER-IREF>
                </ASSEMBLY-SW-CONNECTOR>
              </CONNECTORS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>CompositionsB</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>CompB</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>SwcB_1</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/ComponentsB/SwcB</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>CompositionsTop</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Top</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>CompA_1</SHORT-NAME>
                  <TYPE-TREF DEST="COMPOSITION-SW-COMPONENT-TYPE">/Acme/CompositionsA/CompA</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>CompB_1</SHORT-NAME>
                  <TYPE-TREF DEST="COMPOSITION-SW-COMPONENT-TYPE">/Acme/CompositionsB/CompB</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: The same export with the stub end naming a port under ANOTHER package's
#: ``Ghost``, a path the context component's type does not own ([TPS_GST_00162]),
#: so the end is refused and its connector skipped.
UNOWNED_STUB_PORT_PACKAGES_ARXML = ONTO_STUB_PACKAGES_ARXML.replace(
    "/Elsewhere/Ghost/rIn", "/Elsewhere/Other/Ghost/rIn"
)


NOTHING_IMPORTABLE_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <ELEMENTS>
        <CONSTANT-SPECIFICATION>
          <SHORT-NAME>UnusedConstant</SHORT-NAME>
        </CONSTANT-SPECIFICATION>
        <ACME-EXPORT-METADATA>
          <SHORT-NAME>Meta</SHORT-NAME>
        </ACME-EXPORT-METADATA>
      </ELEMENTS>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


# Helpers


def _init_project(tmp_path: Path, name: str = "proj") -> Path:
    """Scaffold a project through the real ``swcraft init`` CLI path; return the project root."""
    import sys

    target = tmp_path / name
    argv = sys.argv
    try:
        sys.argv = ["swcraft", "init", str(target)]
        main()
    finally:
        sys.argv = argv
    return target


def _model_root(project_root: Path) -> Path:
    return project_root / "model"


# End-to-end: composition_nested.arxml


class TestRunImportEndToEnd:
    def test_full_import_totals_match_fixture_content(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert report.imported_data_types == 2  # uint16, SensorValue_impl
        assert report.imported_interfaces == 1  # ISensorValue
        # ONE file per TYPE: `SensorSwc_Rear` is the fixture's second member of
        # the same type, which the importer skip+reports.
        assert report.imported_swcs == 2
        assert report.imported_connectors == 1
        assert report.imported_compositions == 2
        assert report.total_imported == 8

        # 2 data types + 1 interface + 2 SWCs + 1 connector + 2 compositions = 8.
        assert len(report.written_paths) == 8
        assert all(Path(p).is_file() for p in report.written_paths)

    def test_one_swc_yaml_per_type_and_the_prototypes_are_the_compositions_members(self, tmp_path: Path) -> None:
        """One file per type, and the prototype's ``SHORT-NAME`` is a member's ``name``.

        A type placed twice in one composition is refused by
        ``rule:composition.member-type-repeated``, so one member survives.
        """
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        components = sorted(p.name for p in (model_root / "swc" / "Components").glob("*.yaml"))
        assert components == ["ActuatorSwc.yaml", "SensorSwc.yaml"]

        inner = yaml.safe_load((model_root / "compositions" / "InnerComposition.yaml").read_text(encoding="utf-8"))
        assert inner["members"] == [{"name": "SensorSwc_Front", "type": "SensorSwc"}]
        assert inner["delegations"] == [
            {"name": "DEL_Front", "port": "pFrontValue", "member": "SensorSwc_Front", "member_port": "pValue"}
        ]

        connector_files = list((model_root / "connectors").glob("*.yaml"))
        assert len(connector_files) == 1
        connector_data = yaml.safe_load(connector_files[0].read_text(encoding="utf-8"))
        assert connector_data["from"] == {"composition": "InnerComposition", "port": "pFrontValue"}
        assert connector_data["to"] == {"swc": "ActuatorSwc", "port": "rValue"}
        assert connector_data["composition"] == "OuterComposition"

        # A green import must also be a green model: the importer writes the
        # hierarchy itself, so the connector's endpoints are members unaided.
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_the_scaffolded_root_is_replaced_by_the_imported_one(self, tmp_path: Path) -> None:
        """The scaffolded empty root is removed and reported, or the project would hold two roots."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        assert (model_root / "compositions" / "Root.yaml").is_file()

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert not (model_root / "compositions" / "Root.yaml").exists()
        assert [p.name for p in sorted((model_root / "compositions").glob("*.yaml"), key=lambda p: p.as_posix())] == [
            "InnerComposition.yaml",
            "OuterComposition.yaml",
        ]
        assert report.replaced_scaffold_lines == [
            "compositions/Root.yaml: the empty root composition 'Root' was removed and replaced by the root "
            "this import mapped"
        ]
        assert "Replaced scaffolded compositions:" in report.render()

    def test_the_removal_line_names_a_storage_name_and_no_absolute_path(self, tmp_path: Path) -> None:
        """The removal line names the file relative to the model root, never where the project is stored."""
        project = _init_project(tmp_path)

        rendered = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml")).render()

        assert "  - compositions/Root.yaml: the empty root composition 'Root' was removed" in rendered
        for spelling in {str(tmp_path), tmp_path.as_posix(), str(project), project.as_posix()}:
            assert spelling not in rendered

    def test_the_import_writes_no_requirement_and_no_link(self, tmp_path: Path) -> None:
        """No requirement file, no ``satisfies``, and one sentence saying so, never a warning."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert list((model_root / "requirements").glob("*.yaml")) == []
        for path in (model_root / "swc").rglob("*.yaml"):
            assert "satisfies" not in yaml.safe_load(path.read_text(encoding="utf-8")), path.name
        for path in (model_root / "compositions").glob("*.yaml"):
            assert "satisfies" not in yaml.safe_load(path.read_text(encoding="utf-8")), path.name

        rendered = report.render()
        assert NO_REQUIREMENT_SENTENCE in rendered
        assert "warning" not in NO_REQUIREMENT_SENTENCE.casefold()
        assert "baseline requirement" not in rendered.casefold()

        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues
        assert result.warnings == ()

    def test_a_root_composition_the_architect_linked_survives_the_import(self, tmp_path: Path) -> None:
        """``satisfies`` is content, so a linked scaffold root is kept rather than deleted with its links.

        The project then holds two roots and ``rule:composition.multiple-roots`` reports them.
        """
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        root_file = model_root / "compositions" / "Root.yaml"
        document = yaml.safe_load(root_file.read_text(encoding="utf-8"))
        document["satisfies"] = ["REQ_ROOT@1"]
        root_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n")

        # The emptiness guard counts the links as content too, so the import needs `force`.
        with pytest.raises(ValueError, match="is not empty"):
            run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), force=True)

        assert root_file.is_file()
        assert yaml.safe_load(root_file.read_text(encoding="utf-8"))["satisfies"] == ["REQ_ROOT@1"]
        assert report.replaced_scaffold_lines == []
        roots = [
            path.name
            for path in sorted((model_root / "compositions").glob("*.yaml"), key=lambda candidate: candidate.as_posix())
            if yaml.safe_load(path.read_text(encoding="utf-8")).get("root")
        ]
        assert roots == ["OuterComposition.yaml", "Root.yaml"]

    def test_check_cross_layer_consistency_passes_after_import(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues


# The import writes no key `rule:model.unknown-key` refuses

_FIXTURE_ROOT = Path(__file__).parent / "fixtures"

#: Every synthetic export `import model` takes whole; the one left out is refused
#: by the import itself and writes nothing to look at.
_REFUSED_BY_THE_IMPORT = {"cyclic_composition_graph.arxml"}
_SYNTHETIC_EXPORTS = sorted(
    (
        path
        for directory in (
            "arxml",
            "arxml_import_shapes",
            "arxml_external_stubs",
            "operation_events",
            "component_exchange/earlier_release",
        )
        for path in (_FIXTURE_ROOT / directory).glob("*.arxml")
        if path.name not in _REFUSED_BY_THE_IMPORT
    ),
    key=lambda path: path.as_posix(),
)


class TestTheImportWritesNoUndeclaredKey:
    """`check_unknown_keys` is empty after `import model` of every synthetic export.

    Does not reach: an export shape no fixture holds, and the real tool exports
    (their closed suites carry the same assertion).
    """

    def test_the_corpus_is_the_fixture_directories(self) -> None:
        # The count holds the corpus to the directories.
        assert len(_SYNTHETIC_EXPORTS) > 40

    @pytest.mark.parametrize("export", _SYNTHETIC_EXPORTS, ids=lambda path: path.name)
    def test_no_written_file_holds_an_undeclared_key(self, tmp_path: Path, export: Path) -> None:
        project = _init_project(tmp_path)
        run_import(str(export), str(project / "swcraft.yaml"))

        assert check_unknown_keys(str(_model_root(project))).issues == []


# Writer-identical dump style


class TestWriterIdenticalDumpStyle:
    def test_swc_yaml_byte_identical_to_writer_output(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path, name="imported")
        run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))
        imported_file = _model_root(project) / "swc" / "Components" / "ActuatorSwc.yaml"

        # The same shape built through the writer package's mutation functions in a scratch
        # model_root (`create_swc` takes no termination value, so one is added).
        scratch_root = tmp_path / "scratch_model"
        (scratch_root / "swc").mkdir(parents=True)
        declare_no_support(create_swc(str(scratch_root), "ActuatorSwc", "Components"))
        add_port(str(scratch_root), "ActuatorSwc", "rValue", "require", "ISensorValue")
        scratch_file = scratch_root / "swc" / "Components" / "ActuatorSwc.yaml"

        assert imported_file.read_bytes() == scratch_file.read_bytes()

    def test_interface_yaml_byte_identical_to_writer_output(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path, name="imported")
        run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))
        imported_file = _model_root(project) / "interfaces" / "ISensorValue.yaml"

        scratch_root = tmp_path / "scratch_model"
        (scratch_root / "interfaces").mkdir(parents=True)
        create_interface(
            str(scratch_root),
            "ISensorValue",
            [{"name": "value", "implementation_type": "SensorValue_impl"}],
        )
        scratch_file = scratch_root / "interfaces" / "ISensorValue.yaml"

        assert imported_file.read_bytes() == scratch_file.read_bytes()

    def test_data_type_yaml_byte_identical_to_writer_output(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path, name="imported")
        run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))
        imported_file = model_element_path(_model_root(project), "uint16", kind="base_type")

        scratch_root = tmp_path / "scratch_model"
        (scratch_root / "data_types").mkdir(parents=True)
        create_data_type(
            str(scratch_root), "uint16", "base_type", size=16, encoding="NONE", native_declaration="uint16"
        )
        scratch_file = model_element_path(scratch_root, "uint16", kind="base_type")

        assert imported_file.read_bytes() == scratch_file.read_bytes()


#: The element files `swcraft init` writes. "The importer wrote nothing" is stated
#: as this exact scaffold, so a stray write into a seeded layer is still caught.
SCAFFOLDED_ELEMENT_FILES = [
    "compositions/Root.yaml",
    "hardware/CORE0.yaml",
    "hardware/CPU.yaml",
    "hardware/PARTITION0.yaml",
]


def written_by_the_scaffold() -> dict[str, list[str]]:
    """The scaffolded element filenames, per layer directory, read off `swcraft init`'s file plan.

    Derived, not declared: a hand-written table is a two-layer enumeration
    `test_layer_vocabulary_gate.py` refuses in a fixture module.
    """
    per_layer: dict[str, list[str]] = {}
    for relative in _default_file_plan("proj"):
        head, _, rest = relative.partition("/")
        if head != "model" or "/" not in rest:
            continue
        layer, _, filename = rest.partition("/")
        per_layer.setdefault(layer, []).append(filename)
    return {layer: sorted(names) for layer, names in per_layer.items()}


def _written_element_files(model_root: Path) -> list[str]:
    """Every element file under *model_root*, POSIX-relative to it and sorted."""
    return sorted(path.relative_to(model_root).as_posix() for path in model_root.rglob("*.yaml"))


# --dry-run writes nothing


class TestRunImportDryRun:
    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)

        assert report.total_imported == 8
        assert report.dry_run is True
        # Every declared layer: "writes nothing" is a claim about the whole tree, and
        # the seeded layers must hold the scaffold's files and no others.
        assert written_by_the_scaffold(), "the scaffold seeds no layer; this comparison has no subject"
        for sub in layer_directories():
            written = sorted(path.name for path in (model_root / sub).rglob("*.yaml"))
            assert written == written_by_the_scaffold().get(sub, []), sub


# Non-empty project refusal / --force


class TestRunImportForceRefusal:
    def _seed_one_swc(self, model_root: Path) -> None:
        (model_root / "swc").mkdir(parents=True, exist_ok=True)
        (model_root / "swc" / "PreExisting.yaml").write_text(
            yaml.safe_dump(
                {"name": "PreExisting", "type": "application", "ports": [], "runnables": []},
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def test_non_empty_without_force_refuses(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        self._seed_one_swc(model_root)

        with pytest.raises(ValueError, match="not empty"):
            run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert list((model_root / "interfaces").glob("*.yaml")) == []
        assert list((model_root / "data_types").rglob("*.yaml")) == []
        assert list((model_root / "connectors").glob("*.yaml")) == []
        assert list((model_root / "requirements").glob("*.yaml")) == []

    def test_a_tasks_only_project_is_not_empty(self, tmp_path: Path) -> None:
        """The guard covers every layer the reader reads but `hardware`, not just the ones this importer writes into."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        (model_root / "tasks").mkdir(parents=True, exist_ok=True)
        (model_root / "tasks" / "Task_10ms.yaml").write_text(
            yaml.safe_dump(
                {"name": "Task_10ms", "period": 0.01, "priority": 10, "partition": "PARTITION0"},
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="not empty"):
            run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert list((model_root / "swc").rglob("*.yaml")) == []

    @pytest.mark.parametrize(
        ("layer", "document"),
        [
            ("units", {"name": "ProbeKmh", "display_name": "km/h"}),
            ("compu_methods", {"name": "ProbeTable", "kind": "texttable", "entries": [{"name": "P", "value": 0}]}),
            ("data_constraints", {"name": "ProbeLimits", "max": 1}),
        ],
    )
    def test_a_project_holding_one_element_alone_is_not_empty(
        self, tmp_path: Path, layer: str, document: dict[str, object]
    ) -> None:
        """The three data description element layers are counted by the guard."""
        from swcraft_tools.model_registry import current_registry

        project = _init_project(tmp_path)
        model_root = _model_root(project)
        directory = current_registry().storage(layer).directory
        (model_root / directory).mkdir(parents=True, exist_ok=True)
        (model_root / directory / f"{document['name']}.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8", newline="\n"
        )

        with pytest.raises(ValueError, match="not empty"):
            run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert list((model_root / "swc").rglob("*.yaml")) == []

    def test_a_freshly_scaffolded_project_is_still_empty_enough_to_import_into(self, tmp_path: Path) -> None:
        """Neither layer `swcraft init` seeds refuses an import.

        `hardware/` is excluded whole (the importer never writes there);
        `compositions/` is read for content: a composition declaring a port, a member
        or a delegation refuses, an empty one does not.
        """
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        assert sorted(path.name for path in (model_root / "hardware").glob("*.yaml")) == [
            "CORE0.yaml",
            "CPU.yaml",
            "PARTITION0.yaml",
        ]
        assert sorted(path.name for path in (model_root / "compositions").glob("*.yaml")) == ["Root.yaml"]

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert report.total_imported == 8

    def test_a_hand_authored_hierarchy_refuses_the_import_without_force(self, tmp_path: Path) -> None:
        """A composition that declares anything is content the guard protects; only the empty scaffold passes."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        (model_root / "compositions" / "Root.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "Root",
                    "title": "Root composition",
                    "root": True,
                    "members": [{"name": "Existing", "type": "PreExisting"}],
                },
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="not empty"):
            run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert list((model_root / "swc").rglob("*.yaml")) == []
        assert yaml.safe_load((model_root / "compositions" / "Root.yaml").read_text(encoding="utf-8"))["members"] == [
            {"name": "Existing", "type": "PreExisting"}
        ]

    def test_a_hand_authored_hardware_layer_is_imported_into_without_force(self, tmp_path: Path) -> None:
        """A hand-authored `hardware/` tree alone is imported into without `--force`, and survives byte for byte.

        Exact bytes, because a rewritten file of the same name is the failure.
        """
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        hardware = model_root / "hardware"
        for name, document in (
            ("CORE1", {"name": "CORE1", "kind": "core", "threshold": 0.6}),
            ("CORE2", {"name": "CORE2", "kind": "core", "threshold": 0.6}),
            (
                "RAM0",
                {
                    "name": "RAM0",
                    "kind": "memory",
                    "type": "RAM",
                    "size": 65536,
                    "accessible": ["CORE0", "CORE1", "CORE2"],
                    "threshold": 0.8,
                },
            ),
        ):
            (hardware / f"{name}.yaml").write_text(
                yaml.safe_dump(document, default_flow_style=False, sort_keys=False), encoding="utf-8"
            )
        before = {
            path.name: path.read_bytes() for path in sorted(hardware.glob("*.yaml"), key=lambda path: path.as_posix())
        }
        # The three scaffolded elements (processor, core, partition) and the three written here.
        assert len(before) == 6, sorted(before)

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert report.total_imported == 8
        after = {
            path.name: path.read_bytes() for path in sorted(hardware.glob("*.yaml"), key=lambda path: path.as_posix())
        }
        assert after == before, "the importer wrote into `hardware/`, the layer the guard excludes"

    def test_non_empty_with_force_proceeds(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        self._seed_one_swc(model_root)

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), force=True)

        assert report.total_imported == 8
        assert (model_root / "interfaces" / "ISensorValue.yaml").is_file()


# --packages dependency closure


class TestRunImportPackagesClosure:
    def test_the_closure_follows_a_reference_types_base_down_its_chain(self) -> None:
        """The `--packages` closure walks a reference type's `base` as it walks a value type's."""
        from swcraft_tools.arxml_import.report import closure_data_type_names
        from swcraft_tools.schema import BaseDataType, DataElement, ReferenceDataType, SRInterface, ValueDataType

        data_types = [
            BaseDataType(name="u8", kind="base_type", size=8, encoding="NONE"),
            ValueDataType(name="Raw", kind="value_type", base="u8"),
            ReferenceDataType(name="Level", kind="reference_type", base="Raw"),
            ReferenceDataType(name="LevelRef", kind="reference_type", base="Level"),
            ValueDataType(name="Unused", kind="value_type", base="u8"),
        ]
        interface = SRInterface(
            name="ILevel", kind="sender_receiver", data_elements=[DataElement(name="v", implementation_type="LevelRef")]
        )

        assert closure_data_type_names([interface], data_types) == {
            ("implementation_types", "LevelRef"),
            ("implementation_types", "Level"),
            ("implementation_types", "Raw"),
            ("base_types", "u8"),
        }

    def test_packages_selection_filters_with_dependency_closure(self, tmp_path: Path) -> None:
        source = tmp_path / "two_groups.arxml"
        source.write_text(TWO_GROUPS_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        report = run_import(str(source), str(project / "swcraft.yaml"), packages=["GroupA"])

        assert report.imported_swcs == 1
        assert report.imported_interfaces == 1
        assert report.packages == ["GroupA"]
        assert report.closure_lines

        swc_file = model_root / "swc" / "Components" / "SwcA.yaml"
        assert swc_file.is_file()
        assert not (model_root / "swc" / "Components" / "SwcB.yaml").exists()
        assert (model_root / "interfaces" / "IGroupA.yaml").is_file()
        assert not (model_root / "interfaces" / "IGroupB.yaml").exists()
        # `uint8A_impl` is a value type over `uint8A`, so the closure carries the base type too.
        assert (model_element_path(model_root, "uint8A", kind="base_type")).is_file()
        assert (model_element_path(model_root, "uint8A_impl", kind="value_type")).is_file()
        assert not (model_element_path(model_root, "uint8B_impl", kind="value_type")).exists()
        assert not (model_element_path(model_root, "uint8B", kind="base_type")).exists()

        assert list((model_root / "requirements").glob("*.yaml")) == []

        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_packages_cli_flag_comma_space_separated_string_is_split_and_stripped(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The raw `--packages "GroupA, GroupB"` string is split on commas and stripped through the real CLI."""
        source = tmp_path / "two_groups.arxml"
        source.write_text(TWO_GROUPS_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(source),
                "--project",
                str(project / "swcraft.yaml"),
                "--packages",
                "GroupA, GroupB",
            ],
        )
        main()  # must not raise SystemExit

        assert (model_root / "swc" / "Components" / "SwcA.yaml").is_file()
        assert (model_root / "swc" / "Components" / "SwcB.yaml").is_file()

    def test_packages_selection_matching_nothing_is_import_error(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        with pytest.raises(ValueError, match="matched no SWC"):
            run_import(
                str(COMPOSITION_NESTED),
                str(project / "swcraft.yaml"),
                packages=["NoSuchPackage"],
            )

    def test_packages_selection_of_composition_package_recursively_pulls_in_leaf_swcs(self, tmp_path: Path) -> None:
        """Selecting a composition package pulls in its leaf SWCs and their interfaces across packages."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), packages=["Compositions"])

        assert (model_root / "swc" / "Components" / "SensorSwc.yaml").is_file()
        assert (model_root / "swc" / "Components" / "ActuatorSwc.yaml").is_file()
        assert report.imported_swcs == 2
        # A member naming a dropped component would be refused by
        # `rule:composition.member-unknown-type`, so the members are pruned.
        assert sorted(p.stem for p in (model_root / "compositions").glob("*.yaml")) == [
            "InnerComposition",
            "OuterComposition",
        ]
        assert check_cross_layer_consistency(model_root=str(model_root)).consistent

        # `uint16` is pulled in: `SensorValue_impl`'s `base` names it.
        assert (model_root / "interfaces" / "ISensorValue.yaml").is_file()
        assert (model_element_path(model_root, "SensorValue_impl", kind="value_type")).is_file()
        assert (model_element_path(model_root, "uint16", kind="base_type")).is_file()
        assert report.imported_interfaces == 1
        assert report.imported_data_types == 2

        assert report.imported_connectors == 1

        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues


class TestRunImportPathTraversalGroupGuard:
    """A parent AR-PACKAGE SHORT-NAME of ".." does not let the write escape `model_root/swc/`.

    Checked on the filesystem after the full `run_import()`, not only in the skip log.
    """

    def test_traversal_group_swc_skipped_no_file_escapes_model_root(self, tmp_path: Path) -> None:
        source = tmp_path / "traversal_group.arxml"
        source.write_text(TRAVERSAL_GROUP_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert report.imported_swcs == 1
        assert (model_root / "swc" / "Components" / "GoodSwc.yaml").is_file()

        assert any("EvilSwc" in line for line in report.skip_lines)
        assert any("escape" in line.lower() for line in report.skip_lines if "EvilSwc" in line)

        # In particular not at model_root/EvilSwc.yaml, where `root / "swc" / ".."`
        # would have resolved.
        escaped_target = model_root / "EvilSwc.yaml"
        assert not escaped_target.exists()
        project_root = project
        all_written_names = {p.name for p in project_root.rglob("EvilSwc.yaml")}
        assert all_written_names == set()

        for written in report.written_paths:
            written_path = Path(written)
            assert written_path.is_file()
            assert written_path.resolve().is_relative_to(model_root.resolve())

        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues


class TestRunImportPathTraversalPrototypeNameGuard:
    """A prototype ``SHORT-NAME`` shaped like ``../Evil`` reaches no filesystem: it is skip+reported."""

    def test_traversal_prototype_name_is_skipped_no_file_escapes_group_dir(self, tmp_path: Path) -> None:
        source = tmp_path / "traversal_prototype.arxml"
        source.write_text(PROTOTYPE_NAME_TRAVERSAL_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert report.imported_swcs == 1
        assert (model_root / "swc" / "Components" / "SensorSwc.yaml").is_file()

        assert any("../Evil" in line or "Evil" in line for line in report.skip_lines)
        members = [
            member
            for path in (model_root / "compositions").glob("*.yaml")
            for member in yaml.safe_load(path.read_text(encoding="utf-8"))["members"]
        ]
        assert [m["name"] for m in members] == ["SensorGood"]

        escaped_target = model_root / "swc" / "Evil.yaml"
        assert not escaped_target.exists()
        assert {p.name for p in project.rglob("*Evil*")} == set()

        for written in report.written_paths:
            written_path = Path(written)
            assert written_path.is_file()
            assert written_path.resolve().is_relative_to(model_root.resolve())

        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues


# zero-elements-imported -> exit non-zero; skips-alone -> exit 0 (CLI level)


class TestCliImportExitCodes:
    def test_zero_elements_imported_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "nothing.arxml"
        source.write_text(NOTHING_IMPORTABLE_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(source),
                "--project",
                str(project / "swcraft.yaml"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_zero_elements_imported_writes_nothing(self, tmp_path: Path) -> None:
        """A zero-import run writes nothing."""
        source = tmp_path / "nothing.arxml"
        source.write_text(NOTHING_IMPORTABLE_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert report.total_imported == 0
        assert report.written_paths == []
        assert read_requirements(str(model_root)) == []
        assert list((model_root / "requirements").glob("*.yaml")) == []

    def test_zero_elements_imported_run_does_not_lock_out_legitimate_retry(self, tmp_path: Path) -> None:
        """A retry after a zero-import run succeeds WITHOUT --force: the failed run left nothing to trip the guard."""
        source = tmp_path / "nothing.arxml"
        source.write_text(NOTHING_IMPORTABLE_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)

        failed_report = run_import(str(source), str(project / "swcraft.yaml"))
        assert failed_report.total_imported == 0

        retry_report = run_import(str(UNSUPPORTED_CONTENT), str(project / "swcraft.yaml"))
        assert retry_report.total_imported > 0

    def test_skips_alone_exits_zero(self, tmp_path: Path, monkeypatch) -> None:
        """A non-empty skip list alone does not fail the import."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(UNSUPPORTED_CONTENT),
                "--project",
                str(project / "swcraft.yaml"),
            ],
        )
        main()  # must not raise SystemExit

        assert (model_root / "swc" / "Components" / "DiagnosticBridgeSwc.yaml").is_file()
        assert (model_root / "interfaces" / "IDiagnosticChannel.yaml").is_file()

    def test_skips_alone_report_lists_skip_reasons(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(UNSUPPORTED_CONTENT), str(project / "swcraft.yaml"))

        assert report.total_imported > 0
        assert report.total_skipped >= 3  # PR-port, unknown vendor tag, CONSTANT-SPECIFICATION
        tags = set(report.skipped_by_tag)
        assert "PR-PORT-PROTOTYPE" in tags
        assert "ACME-EXPORT-METADATA" in tags
        assert "CONSTANT-SPECIFICATION" in tags

    def test_a_constant_specifications_skip_reason_says_where_its_value_went(self, tmp_path: Path) -> None:
        """A constant is one skip line, whose sentence says where its value went.

        There is no constants layer; its value is carried where an INIT-VALUE references it.
        """
        project = _init_project(tmp_path)
        report = run_import(str(UNSUPPORTED_CONTENT), str(project / "swcraft.yaml"))

        lines = [line for line in report.skip_lines if line.startswith("CONSTANT-SPECIFICATION ")]
        assert len(lines) == 1
        assert "is not an SWCraft element" in lines[0]
        assert "INIT-VALUE references it" in lines[0]
        assert "has no SWCraft representation" not in lines[0]


# cmd_import's CLI-level error handling, through the wrapper in cli/import_commands.py


class TestCliImportErrorHandling:
    def test_unsupported_format_value_rejected_by_argparse_choices(self, tmp_path: Path, monkeypatch) -> None:
        """Argparse's closed `choices=` rejects an unsupported format with exit 2 before `cmd_import` runs."""
        project = _init_project(tmp_path)

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "not-a-real-format",
                "--source",
                str(COMPOSITION_NESTED),
                "--project",
                str(project / "swcraft.yaml"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2

    def test_missing_source_exits_nonzero(self, tmp_path: Path, monkeypatch) -> None:
        project = _init_project(tmp_path)

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--project",
                str(project / "swcraft.yaml"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_run_import_error_propagates_through_cmd_import_to_nonzero_exit(self, tmp_path: Path, monkeypatch) -> None:
        """A `run_import()` error (a non-empty project without `--force`) is a clean non-zero exit, not a traceback."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        (model_root / "swc").mkdir(parents=True, exist_ok=True)
        (model_root / "swc" / "PreExisting.yaml").write_text(
            yaml.safe_dump(
                {"name": "PreExisting", "type": "application", "ports": [], "runnables": []},
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(COMPOSITION_NESTED),
                "--project",
                str(project / "swcraft.yaml"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


# The FORMAT value is a closed set of two words


class TestTheFormatArgumentIsAClosedSetOfTwoWords:
    """`model` and `component`, and nothing else, through the real ``main()``.

    The removed alias ``rhapsody`` is refused the way any other third word is.
    """

    @staticmethod
    def _argv(project: Path, form: str) -> list[str]:
        return [
            "swcraft",
            "import",
            form,
            "--source",
            str(COMPOSITION_NESTED),
            "--project",
            str(project / "swcraft.yaml"),
        ]

    def test_the_removed_alias_is_argparse_exit_2_naming_what_is_accepted(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """`rhapsody` is refused with exit 2, a usage line and the two accepted words.

        The words are asserted as substrings: CPython rewords argparse's sentence between releases.
        """
        project = _init_project(tmp_path)
        monkeypatch.setattr("sys.argv", self._argv(project, "rhapsody"))
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2

        err = capsys.readouterr().err
        assert "usage:" in err, err
        assert "rhapsody" in err, err
        assert "model" in err and "component" in err, err

    def test_a_word_that_was_never_accepted_is_refused_the_same_way(self, tmp_path: Path, monkeypatch) -> None:
        """Non-vacuity: exit 2 is the closed set's answer and not the alias's own."""
        project = _init_project(tmp_path)
        monkeypatch.setattr("sys.argv", self._argv(project, "rhapsody-export"))
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2

    def test_import_model_says_nothing_on_stderr(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A successful `import model` is silent on stderr."""
        project = _init_project(tmp_path)
        monkeypatch.setattr("sys.argv", self._argv(project, "model"))
        main()
        assert capsys.readouterr().err == ""


# --report writes the rendered text to a file


class TestCliImportReportFile:
    def test_report_flag_writes_rendered_text(self, tmp_path: Path, monkeypatch) -> None:
        project = _init_project(tmp_path)
        report_path = tmp_path / "report.txt"

        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(COMPOSITION_NESTED),
                "--project",
                str(project / "swcraft.yaml"),
                "--report",
                str(report_path),
            ],
        )
        main()  # must not raise SystemExit

        assert report_path.is_file()
        rendered = report_path.read_text(encoding="utf-8")
        assert "IMPORTED 2 SWCs" in rendered
        assert "IMPORTED 2 compositions" in rendered

    def test_a_markdown_path_still_gets_the_text(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A ``.md`` path gets the text report: ``import model`` has no Markdown builder (``cli.is_markdown_report``)."""
        project = _init_project(tmp_path)
        capsys.readouterr()
        report_path = tmp_path / "report.md"
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "model",
                "--source",
                str(COMPOSITION_NESTED),
                "--project",
                str(project / "swcraft.yaml"),
                "--report",
                str(report_path),
            ],
        )
        main()  # must not raise SystemExit

        assert report_path.read_text(encoding="utf-8") == capsys.readouterr().out.removesuffix("\n")


# AUTOSAR 4.3.0 input


def _relabelled_composition(tmp_path: Path) -> Path:
    """``composition_nested.arxml``, relabelled 4.3.0, written under *tmp_path*.

    A relabel, not a fixture twin: the composition fixture reaches every layer, and
    version-*label* tolerance is the property under test.
    """
    relabelled = tmp_path / "composition_nested_4_3_0.arxml"
    relabelled.write_text(
        relabel_arxml_version(
            COMPOSITION_NESTED.read_text(encoding="utf-8"),
            source=ArxmlVersion.V4_2_2.schema_filename,
            target=ArxmlVersion.V4_3_0.schema_filename,
        ),
        encoding="utf-8",
    )
    return relabelled


def _model_tree(model_root: Path) -> dict[str, str]:
    """Every YAML file under a model root, keyed by its path relative to that root."""
    return {
        path.relative_to(model_root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(model_root.rglob("*.yaml"), key=lambda path: path.as_posix())
    }


class TestRunImportAt4_3_0:
    """``swcraft import model`` maps a 4.3.0-labelled source across every layer, file for file as 4.2.2.

    Does not reach: a real 4.3.0 export's dialect; the fixture is hand-crafted to SWCraft's own shapes.
    """

    def test_writes_the_same_model_tree_as_the_4_2_2_source(self, tmp_path: Path) -> None:
        at_4_2_2 = _init_project(tmp_path, name="proj_4_2_2")
        at_4_3_0 = _init_project(tmp_path, name="proj_4_3_0")

        run_import(str(COMPOSITION_NESTED), str(at_4_2_2 / "swcraft.yaml"))
        run_import(str(_relabelled_composition(tmp_path)), str(at_4_3_0 / "swcraft.yaml"))

        assert _model_tree(_model_root(at_4_3_0)) == _model_tree(_model_root(at_4_2_2))

    def test_every_layer_is_imported(self, tmp_path: Path) -> None:
        """The counts, spelled out — two empty trees also compare equal."""
        project = _init_project(tmp_path)

        report = run_import(str(_relabelled_composition(tmp_path)), str(project / "swcraft.yaml"))

        assert report.imported_data_types == 2
        assert report.imported_interfaces == 1
        assert report.imported_swcs == 2
        assert report.imported_connectors == 1
        assert report.imported_compositions == 2
        assert report.total_imported == 8
        assert read_requirements(str(_model_root(project))) == []

    def test_the_imported_model_is_consistent(self, tmp_path: Path) -> None:
        """Cross-layer consistency, the same bar the 4.2.2 import is held to."""
        project = _init_project(tmp_path)

        run_import(str(_relabelled_composition(tmp_path)), str(project / "swcraft.yaml"))

        result = check_cross_layer_consistency(model_root=str(_model_root(project)))
        assert result.consistent, result.issues


class TestTheReportNamesAVersionTheManifestDoesNotHold:
    """``import model`` never changes the manifest; its report says when the source's version differs.

    The line is path-free (the schema file name is the only file name it carries).
    Does not reach: a source declaring no schema, or a schema SWCraft writes no release
    of, gets no line; ``import component`` prints no such line.
    """

    @staticmethod
    def _version_lines(rendered: str) -> list[str]:
        return [line for line in rendered.splitlines() if line.startswith("ARXML version:")]

    def test_a_4_3_0_source_into_a_4_2_2_project_names_both_versions(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        manifest = project / "swcraft.yaml"
        before = manifest.read_text(encoding="utf-8")

        report = run_import(str(_relabelled_composition(tmp_path)), str(manifest))

        assert manifest.read_text(encoding="utf-8") == before
        assert self._version_lines(report.render()) == [
            "ARXML version: the file declares AUTOSAR 4.3.0 (AUTOSAR_4-3-0.xsd); this project's "
            "arxml_version is 4.2.2, and import model does not change it -- set arxml_version: 4.3.0 "
            "in swcraft.yaml, or check-model applies 4.2.2's rules."
        ]

    def test_a_4_2_2_source_into_a_4_3_0_project_names_the_pair_the_other_way(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        manifest = project / "swcraft.yaml"
        text = manifest.read_text(encoding="utf-8")
        assert "arxml_version" not in text, text  # the scaffold states none: 4.2.2 by default
        manifest.write_text(text + "arxml_version: 4.3.0\n", encoding="utf-8", newline="")

        report = run_import(str(COMPOSITION_NESTED), str(manifest))

        assert self._version_lines(report.render()) == [
            "ARXML version: the file declares AUTOSAR 4.2.2 (AUTOSAR_4-2-2.xsd); this project's "
            "arxml_version is 4.3.0, and import model does not change it -- set arxml_version: 4.2.2 "
            "in swcraft.yaml, or check-model applies 4.3.0's rules."
        ]

    def test_an_agreeing_version_prints_no_line(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert self._version_lines(report.render()) == []

    def test_the_line_names_no_location(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(_relabelled_composition(tmp_path)), str(project / "swcraft.yaml"))

        (line,) = self._version_lines(report.render())
        assert str(tmp_path) not in line
        assert tmp_path.as_posix() not in line
        assert "/" not in line and "\\" not in line

    @staticmethod
    def _report(declared_schema: str | None, project_arxml_version: ArxmlVersion | None) -> ImportReport:
        return ImportReport(
            source="export.arxml",
            packages=None,
            dry_run=True,
            imported_data_types=0,
            imported_interfaces=0,
            imported_swcs=0,
            imported_connectors=0,
            imported_compositions=0,
            skipped_by_tag={},
            skip_entries=[],
            reduction_lines=[],
            rename_lines=[],
            init_radix_lines=[],
            closure_lines=[],
            declared_schema=declared_schema,
            project_arxml_version=project_arxml_version,
        )

    @pytest.mark.parametrize(
        ("declared_schema", "project_arxml_version"),
        [
            (None, ArxmlVersion.V4_2_2),
            ("AUTOSAR_00049.xsd", ArxmlVersion.V4_2_2),
            ("AUTOSAR_4-3-0.xsd", None),
        ],
        ids=["no-schema", "a-schema-of-an-unwritten-release", "no-project"],
    )
    def test_no_line_where_the_manifest_could_not_name_the_version_or_there_is_no_project(
        self, declared_schema: str | None, project_arxml_version: ArxmlVersion | None
    ) -> None:
        report = self._report(declared_schema, project_arxml_version)

        assert report.version_line is None
        assert self._version_lines(report.render()) == []

    def test_the_line_is_the_report_s_through_its_two_fields(self) -> None:
        report = self._report("AUTOSAR_4-3-0.xsd", ArxmlVersion.V4_2_2)

        assert report.version_line is not None
        assert self._version_lines(report.render()) == [report.version_line]

    def test_the_cli_prints_the_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _init_project(tmp_path)
        source = _relabelled_composition(tmp_path)
        capsys.readouterr()
        monkeypatch.setattr(
            "sys.argv",
            ["swcraft", "import", "model", "--source", str(source), "--project", str(project / "swcraft.yaml")],
        )

        main()  # must not raise SystemExit

        assert self._version_lines(capsys.readouterr().out) == [
            "ARXML version: the file declares AUTOSAR 4.3.0 (AUTOSAR_4-3-0.xsd); this project's "
            "arxml_version is 4.2.2, and import model does not change it -- set arxml_version: 4.3.0 "
            "in swcraft.yaml, or check-model applies 4.2.2's rules."
        ]


# Reserved-name reconciliation, end to end through run_import()
#
# `test_reserved_name_policies.py` owns the rules; here: the pass runs after
# `--packages` selection, a rename reaches every layer written, and the result
# passes `check-model` and `generate arxml`.

RESERVED_SWC_PAIR_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Components</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Brake</SHORT-NAME>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>Brake_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Brake_Implementation</SHORT-NAME>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>Brake_Implementation_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""
"""A schema-valid export naming a component ``{base}_Implementation`` beside
``{base}`` -- the SWC-side collision a data-type-only reconciliation would miss.
It imported cleanly into a project ``generate arxml`` then refused."""


def _source(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _substituted_data_types(tmp_path: Path, replacement: str) -> Path:
    """``data_types.arxml`` with ``uint16_impl`` renamed to *replacement* at its declaration and references."""
    return _source(
        tmp_path,
        f"data_types_{replacement}.arxml",
        (FIXTURES_DIR / "data_types.arxml").read_text(encoding="utf-8").replace("uint16_impl", replacement),
    )


def _imported_names(model_root: Path, layer: str) -> set[str]:
    return {path.stem for path in (model_root / layer).glob("*.yaml")}


def _imported_type_names(model_root: Path) -> set[str]:
    """The stems of the files under ``data_types/``'s ``implementation/``, ``base/`` and ``application/``."""
    return (
        _imported_names(model_root, "data_types/implementation")
        | _imported_names(model_root, "data_types/base")
        | _imported_names(model_root, "data_types/application")
    )


class TestAReservedNameNeverReachesTheWrittenProject:
    """Every reserved shape, imported: the written project builds.

    Not guaranteed here: a legal declared name whose generated form overflows
    (`TestEveryNameTheImportMintsConforms`).
    """

    @staticmethod
    def _import(tmp_path: Path, source: Path):
        project = _init_project(tmp_path)
        report = run_import(str(source), str(project / "swcraft.yaml"))
        return _model_root(project), report

    def _assert_buildable(self, tmp_path: Path, model_root: Path) -> None:
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues
        generate_arxml(str(model_root), str(tmp_path / "out.arxml"))

    def test_a_source_declared_impl_named_type_keeps_its_name(self, tmp_path: Path) -> None:
        """``{x}_impl`` is no reserved name: it is written unrenamed, and the project builds."""
        model_root, report = self._import(tmp_path, FIXTURES_DIR / "data_types.arxml")

        assert "uint16_impl" in _imported_names(model_root, "data_types/implementation")
        assert "uint16_impl_app" not in _imported_type_names(model_root)
        assert not any("uint16_impl" in line for line in report.rename_lines)
        self._assert_buildable(tmp_path, model_root)

    @pytest.mark.parametrize(
        "name",
        ["WheelTravelRange_impl_AppView", "uint16_AppView", "DataTypeMappingSet"],
        ids=repr,
    )
    def test_a_name_the_writer_no_longer_generates_is_left_alone(self, tmp_path: Path, name: str) -> None:
        """An application-view name over a value or base type, and ``DataTypeMappingSet``, keep their name.

        The writer generates none of them; renaming would make the guard stricter than the writer.
        """
        source = _substituted_data_types(tmp_path, name)

        model_root, report = self._import(tmp_path, source)

        assert name in _imported_names(model_root, "data_types/implementation")
        assert not any(name in line for line in report.rename_lines)
        self._assert_buildable(tmp_path, model_root)

    def test_a_component_pair_that_collides_with_the_generated_implementation(self, tmp_path: Path) -> None:
        source = _source(tmp_path, "swc_pair.arxml", RESERVED_SWC_PAIR_ARXML)

        model_root, report = self._import(tmp_path, source)

        assert _imported_names(model_root, "swc/Components") == {"Brake", "Brake_Implementation_app"}
        assert any("SWC-IMPLEMENTATION" in line for line in report.rename_lines)
        self._assert_buildable(tmp_path, model_root)

    def test_a_component_collision_on_the_fixture_that_has_connectors(self, tmp_path: Path) -> None:
        """A component collision is followed into the composition members, and the project still builds."""
        source = _source(tmp_path, "composition_reserved.arxml", _RESERVED_COMPONENT_EXPORT)

        model_root, _report = self._import(tmp_path, source)

        assert _RESERVED_COMPONENT not in _imported_names(model_root, "swc/Components")
        assert f"{_RESERVED_COMPONENT}_app" in _imported_names(model_root, "swc/Components")
        members = {
            member["type"]
            for path in (model_root / "compositions").glob("*.yaml")
            for member in yaml.safe_load(path.read_text(encoding="utf-8"))["members"]
            if "type" in member
        }
        assert f"{_RESERVED_COMPONENT}_app" in members
        self._assert_buildable(tmp_path, model_root)

    def test_a_component_named_after_a_compositions_implementation_is_left_alone(self, tmp_path: Path) -> None:
        """A composition emits no ``SWC-IMPLEMENTATION``, so its ``_Implementation`` name is free and kept."""
        legal = "InnerComposition_Implementation"
        source = _source(
            tmp_path,
            "composition_impl_legal.arxml",
            COMPOSITION_NESTED.read_text(encoding="utf-8").replace("ActuatorSwc", legal),
        )

        model_root, report = self._import(tmp_path, source)

        assert legal in _imported_names(model_root, "swc/Components")
        assert f"{legal}_app" not in _imported_names(model_root, "swc/Components")
        assert not [line for line in report.rename_lines if legal in line]
        self._assert_buildable(tmp_path, model_root)

    def test_a_component_named_after_an_external_stubs_implementation_is_left_alone(self, tmp_path: Path) -> None:
        """``_emit_swcs`` emits nothing for a stub, so its ``_Implementation`` name is free and kept."""
        source = _source(
            tmp_path,
            "composition_stub_impl.arxml",
            COMPOSITION_NESTED.read_text(encoding="utf-8")
            .replace("ActuatorSwc", "Ghost_Implementation")
            .replace(
                '"APPLICATION-SW-COMPONENT-TYPE">/Acme/Chassis/Components/Ghost_Implementation</TYPE-TREF>',
                '"APPLICATION-SW-COMPONENT-TYPE">/Foreign/Pkg/Ghost</TYPE-TREF>',
            ),
        )

        model_root, report = self._import(tmp_path, source)

        stubs = _imported_names(model_root, "swc/external")
        assert "Ghost_ext0" in stubs, f"the unresolvable type must be imported as a stub; got {stubs}"
        assert "Ghost_Implementation" in _imported_names(model_root, "swc/Components")
        assert "Ghost_Implementation_app" not in _imported_names(model_root, "swc/Components")
        assert not [line for line in report.rename_lines if "SWC-IMPLEMENTATION" in line]
        self._assert_buildable(tmp_path, model_root)

    def test_a_conforming_export_is_imported_unchanged(self, tmp_path: Path) -> None:
        """The pass is inert on an export that declares no reserved name."""
        before = _init_project(tmp_path, name="before")
        run_import(str(COMPOSITION_NESTED), str(before / "swcraft.yaml"))

        after = _init_project(tmp_path, name="after")
        report = run_import(str(COMPOSITION_NESTED), str(after / "swcraft.yaml"))

        assert _model_tree(_model_root(after)) == _model_tree(_model_root(before))
        assert not [line for line in report.rename_lines if "collides" in line or "reserved" in line]


class TestARenamedComponentIsFollowedThroughEveryLayer:
    """The references a component rename has to drag along with it."""

    @staticmethod
    def _imported(tmp_path: Path):
        source = tmp_path / "composition_reserved.arxml"
        source.write_text(_RESERVED_COMPONENT_EXPORT, encoding="utf-8")
        project = _init_project(tmp_path)
        report = run_import(str(source), str(project / "swcraft.yaml"))
        return _model_root(project), report

    def test_the_connector_endpoints_follow(self, tmp_path: Path) -> None:
        model_root, _report = self._imported(tmp_path)

        # Only the component half of an endpoint can be renamed, so its own field is read.
        endpoints: set[str] = set()
        for path in (model_root / "connectors").glob("*.yaml"):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            for side in ("from", "to"):
                endpoints.add(payload[side].get("swc") or payload[side]["composition"])

        assert _RESERVED_COMPONENT not in endpoints
        assert f"{_RESERVED_COMPONENT}_app" in endpoints

    def test_the_connector_name_does_not_move_with_the_component(self, tmp_path: Path) -> None:
        """The importer keeps the export's own connector ``SHORT-NAME``, which a rename does **not** reach."""
        model_root, _report = self._imported(tmp_path)

        names = _imported_names(model_root, "connectors")

        assert names == {"Assembly_Inner_to_Actuator"}
        payload = yaml.safe_load(
            (model_root / "connectors" / "Assembly_Inner_to_Actuator.yaml").read_text(encoding="utf-8")
        )
        assert payload["name"] == "Assembly_Inner_to_Actuator"
        assert payload["to"]["swc"] == f"{_RESERVED_COMPONENT}_app"

    def test_the_renamed_component_carries_no_link(self, tmp_path: Path) -> None:
        """The renamed component is imported without ``satisfies``, and no requirement file is written."""
        model_root, _report = self._imported(tmp_path)

        assert read_requirements(str(model_root)) == []
        renamed = read_swc_model(str(model_root))
        assert f"{_RESERVED_COMPONENT}_app" in {swc.name for swc in renamed}
        assert all(swc.satisfies == [] for swc in renamed)


class TestThePassSeesTheFinalElementSet:
    """The pass runs on the element set selection left standing.

    `_cross_group_collision` adds to GroupA a type that collides only with GroupB's.
    """

    @staticmethod
    def _two_groups(tmp_path: Path) -> Path:
        source = tmp_path / "two_groups.arxml"
        source.write_text(TWO_GROUPS_ARXML, encoding="utf-8")
        return source

    def test_a_full_import_keeps_every_groups_impl_named_type(self, tmp_path: Path) -> None:
        """`{x}` beside `{x}_impl` is no collision, so a full import renames neither group's implementation type."""
        project = _init_project(tmp_path)

        run_import(str(self._two_groups(tmp_path)), str(project / "swcraft.yaml"))

        names = _imported_type_names(_model_root(project))
        assert {"uint8A_impl", "uint8B_impl"} <= names
        assert not {"uint8A_impl_app", "uint8B_impl_app"} & names

    def test_a_dropped_group_reaches_neither_the_files_nor_the_report(self, tmp_path: Path) -> None:
        """`--packages GroupA` drops GroupB whole: none of its types is written and no report line names one."""
        project = _init_project(tmp_path)

        report = run_import(str(self._two_groups(tmp_path)), str(project / "swcraft.yaml"), packages=["GroupA"])

        names = _imported_type_names(_model_root(project))
        assert not {"uint8B", "uint8B_impl", "uint8B_impl_app"} & names
        assert not [line for line in report.rename_lines if "uint8B_impl" in line]
        assert names == {"uint8A", "uint8A_impl"}

    @staticmethod
    def _cross_group_collision(tmp_path: Path) -> Path:
        """`TWO_GROUPS_ARXML` with GroupB declaring an enum and GroupA a type of its generated name.

        GroupB's TEXTTABLE primitive `ModeB` generates `ModeB_CompuMethod`, which GroupA
        declares, so the collision exists only while GroupB is written. An enum because
        `{x}_CompuMethod` is the one data-type name still reserved for a kind; `ModeB`'s
        table states `COMPU-PHYS-TO-INTERNAL`, which no element holds, so it is read
        inline and the form is still generated.
        """
        enum_type = (
            "</IMPLEMENTATION-DATA-TYPE>"
            "<COMPU-METHOD><SHORT-NAME>ModeBScale</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>'
            "<COMPU-CONST><VT>OFF</VT></COMPU-CONST></COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS>"
            "<COMPU-PHYS-TO-INTERNAL><COMPU-SCALES><COMPU-SCALE>"
            "<COMPU-CONST><VT>OFF</VT></COMPU-CONST></COMPU-SCALE></COMPU-SCALES></COMPU-PHYS-TO-INTERNAL>"
            "</COMPU-METHOD>"
            "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>ModeB</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            '<COMPU-METHOD-REF DEST="COMPU-METHOD">/Acme/GroupB/DataTypes/ModeBScale</COMPU-METHOD-REF>'
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>\n              </ELEMENTS>"
        )
        colliding_type = (
            "</IMPLEMENTATION-DATA-TYPE>"
            "<COMPU-METHOD><SHORT-NAME>ModeAScale</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<COMPU-INTERNAL-TO-PHYS><COMPU-SCALES><COMPU-SCALE>"
            '<LOWER-LIMIT INTERVAL-TYPE="CLOSED">0</LOWER-LIMIT>'
            '<UPPER-LIMIT INTERVAL-TYPE="CLOSED">0</UPPER-LIMIT>'
            "<COMPU-CONST><VT>IDLE</VT></COMPU-CONST></COMPU-SCALE></COMPU-SCALES></COMPU-INTERNAL-TO-PHYS>"
            "</COMPU-METHOD>"
            "<APPLICATION-PRIMITIVE-DATA-TYPE><SHORT-NAME>ModeB_CompuMethod</SHORT-NAME><CATEGORY>TEXTTABLE</CATEGORY>"
            "<SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS><SW-DATA-DEF-PROPS-CONDITIONAL>"
            '<COMPU-METHOD-REF DEST="COMPU-METHOD">/Acme/GroupA/DataTypes/ModeAScale</COMPU-METHOD-REF>'
            "</SW-DATA-DEF-PROPS-CONDITIONAL></SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>"
            "</APPLICATION-PRIMITIVE-DATA-TYPE>\n              </ELEMENTS>"
        )
        element = '<TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/GroupA/DataTypes/uint8A_impl</TYPE-TREF>\n                    </VARIABLE-DATA-PROTOTYPE>'
        second_element = (
            "<VARIABLE-DATA-PROTOTYPE><SHORT-NAME>mode</SHORT-NAME>"
            '<TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/GroupA/DataTypes/ModeB_CompuMethod</TYPE-TREF>'
            "</VARIABLE-DATA-PROTOTYPE>"
        )
        type_anchor = "</IMPLEMENTATION-DATA-TYPE>\n              </ELEMENTS>"
        # GroupB is edited FIRST, through the last anchor, because GroupA's
        # insertion re-creates the anchor at its own tail.
        assert TWO_GROUPS_ARXML.count(type_anchor) == 2
        assert TWO_GROUPS_ARXML.count(element) == 1
        head, _sep, tail = TWO_GROUPS_ARXML.rpartition(type_anchor)
        text = head + enum_type + tail
        assert text.count(type_anchor) == 1
        text = text.replace(type_anchor, colliding_type, 1).replace(element, element + second_element, 1)
        source = tmp_path / "cross_group_collision.arxml"
        source.write_text(text, encoding="utf-8", newline="\n")
        return source

    def test_a_collision_only_a_dropped_group_completes_is_not_reconciled(self, tmp_path: Path) -> None:
        """A collision only a dropped group completes is not reconciled: the pass runs after selection."""
        project = _init_project(tmp_path)

        report = run_import(
            str(self._cross_group_collision(tmp_path)), str(project / "swcraft.yaml"), packages=["GroupA"]
        )

        names = _imported_type_names(_model_root(project))
        assert names == {"uint8A", "uint8A_impl", "ModeB_CompuMethod"}
        assert not [line for line in report.rename_lines if "ModeB_CompuMethod" in line]

    def test_the_same_collision_is_reconciled_when_both_groups_are_written(self, tmp_path: Path) -> None:
        """The control: with GroupB written the collision is real and GroupA's type is renamed."""
        project = _init_project(tmp_path)

        report = run_import(str(self._cross_group_collision(tmp_path)), str(project / "swcraft.yaml"))

        names = _imported_type_names(_model_root(project))
        assert "ModeB_CompuMethod_app" in names
        assert "ModeB_CompuMethod" not in names
        assert [line for line in report.rename_lines if "ModeB_CompuMethod" in line]


class TestTheImportReportNamesNoFlattenedTypeChain:
    """A TYPE_REFERENCE type is written as the export declared it, and no report line says a chain was flattened."""

    def test_the_type_reference_is_written_one_step_and_the_report_says_nothing_was_flattened(
        self, tmp_path: Path
    ) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(FIXTURES_DIR / "data_types.arxml"), str(project / "swcraft.yaml"))

        written = yaml.safe_load(
            (model_element_path(_model_root(project), "WheelTravelRange_impl", kind="reference_type")).read_text(
                encoding="utf-8"
            )
        )
        assert (written["kind"], written["base"]) == ("reference_type", "uint16_impl")
        assert "flattened" not in report.render().lower()


class TestEveryNameTheImportMintsConforms:
    """The names the importer *invents*: an external stub's name, routed through the shared length cap.

    Scope: concatenation by this package never produces an out-of-range name. Not
    claimed: every written name is buildable; a declared name whose *generated* form
    overflows is a source-side problem `check_identifier_conformance()` reports and
    the writer refuses.
    """

    #: The longest name an export may legally declare, so anything minted from it
    #: is a *concatenation* overflow.
    _LONG_LEGAL = "S" * MAX_IDENTIFIER_LENGTH

    @classmethod
    def _rewritten_composition(cls, **substitutions: str) -> str:
        """`composition_nested.arxml` with whole tokens substituted, so every reference follows.

        `SensorSwc_Front` and `SensorSwc_Rear` are rewritten before the `SensorSwc` they start with.
        """
        text = COMPOSITION_NESTED.read_text(encoding="utf-8")
        for token in ("SensorSwc_Front", "SensorSwc_Rear", "SensorSwc"):
            if token in substitutions:
                text = text.replace(token, substitutions[token])
        return text

    @classmethod
    def _long_stub_import(cls, tmp_path: Path):
        """An export whose *minted* external-stub name (`<128-character leaf>_ext0`) overflows.

        Every *declared* name is a legal `Identifier`.
        """
        text = COMPOSITION_NESTED.read_text(encoding="utf-8")
        # The prototype first: `{128 characters}_1` would itself be an
        # over-length declared member name, a different case.
        text = text.replace("ActuatorSwc_1", "ActuatorInstance").replace("ActuatorSwc", cls._LONG_LEGAL)
        text = text.replace(
            """<SHORT-NAME>SensorSwc_Rear</SHORT-NAME>
                      <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Chassis/Components/SensorSwc</TYPE-TREF>""",
            f"""<SHORT-NAME>SensorSwc_Rear</SHORT-NAME>
                      <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Elsewhere/Absent/{cls._LONG_LEGAL}</TYPE-TREF>""",
        )
        source = tmp_path / "long_stub.arxml"
        source.write_text(text, encoding="utf-8")
        project = _init_project(tmp_path)
        report = run_import(str(source), str(project / "swcraft.yaml"))
        return _model_root(project), report

    def test_a_minted_external_stub_name_is_capped(self, tmp_path: Path) -> None:
        model_root, report = self._long_stub_import(tmp_path)

        names = _imported_names(model_root, "swc/external")

        assert len(report.external_stub_lines) == 1
        assert len(names) == 1
        minted = next(iter(names))
        assert minted.startswith("S" * 10)
        assert len(minted) == MAX_IDENTIFIER_LENGTH
        assert check_identifier(minted) is None
        assert minted.endswith("_ext0"), (
            "the disambiguating token is what may not be truncated -- the stem loses characters"
        )

    def test_an_over_length_declared_prototype_name_is_refused_not_truncated(self, tmp_path: Path) -> None:
        """An over-length prototype `SHORT-NAME` is refused with nothing written, never capped.

        Capping would import an element under a string the export never contained.
        """
        declared = "P" + "a" * 140
        source = tmp_path / "over_length_prototype.arxml"
        source.write_text(self._rewritten_composition(SensorSwc_Front=declared), encoding="utf-8")
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier") as exc_info:
            run_import(str(source), str(project / "swcraft.yaml"))

        message = str(exc_info.value)
        assert f"composition 'InnerComposition' member '{declared}': {REASON_LENGTH}" in message, message
        assert _written_element_files(_model_root(project)) == SCAFFOLDED_ELEMENT_FILES, (
            "a refusal must leave no partial project"
        )

    def test_a_prototype_refusal_names_the_string_the_export_declared(self, tmp_path: Path) -> None:
        """The message quotes the declared name, which the reader can search the export for."""
        source = tmp_path / "illegal_prototype.arxml"
        source.write_text(
            self._rewritten_composition(
                SensorSwc_Front="Wheel Speed", SensorSwc_Rear="SensorRear", SensorSwc="Wheel Speed"
            ),
            encoding="utf-8",
        )
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier") as exc_info:
            run_import(str(source), str(project / "swcraft.yaml"))

        message = str(exc_info.value)
        assert "SWC 'Wheel Speed':" in message, message
        assert "composition 'InnerComposition' member 'Wheel Speed':" in message, message
        assert "_inner_composition" not in message, message

    def test_the_connector_keeps_the_exports_own_name_however_long_the_types_are(self, tmp_path: Path) -> None:
        """A connector name embeds no component name, so a 128-character type cannot push it out of range."""
        model_root, _report = self._long_stub_import(tmp_path)

        names = _imported_names(model_root, "connectors")

        assert names == {"Assembly_Inner_to_Actuator"}
        for name in names:
            assert check_identifier(name) is None, name

    def test_a_source_short_name_that_is_no_identifier_is_refused_with_nothing_written(self, tmp_path: Path) -> None:
        """A ``SHORT-NAME`` that is no ``IDENTIFIER--SIMPLE`` is refused, as ``import component`` refuses it.

        ``arxml_reader`` does not enforce that facet, and renaming is no repair.
        """
        source = tmp_path / "bad_name.arxml"
        source.write_text(
            (FIXTURES_DIR / "minimal_swc.arxml")
            .read_text(encoding="utf-8")
            .replace("WheelSpeedSensorSwc", "Wheel Speed Swc"),
            encoding="utf-8",
        )
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier") as exc_info:
            run_import(str(source), str(project / "swcraft.yaml"))

        assert "Wheel Speed Swc" in str(exc_info.value)
        assert _written_element_files(_model_root(project)) == SCAFFOLDED_ELEMENT_FILES, (
            "a refusal must leave no partial project"
        )

    def test_a_nested_source_name_is_refused_too(self, tmp_path: Path) -> None:
        """A port, a data element or a data type name is refused as the SWC name is."""
        source = tmp_path / "bad_port.arxml"
        source.write_text(
            (FIXTURES_DIR / "minimal_swc.arxml").read_text(encoding="utf-8").replace("pWheelSpeed", "p Wheel Speed"),
            encoding="utf-8",
        )
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier"):
            run_import(str(source), str(project / "swcraft.yaml"))

    def test_an_over_length_declared_name_is_refused_not_only_a_pattern_illegal_one(self, tmp_path: Path) -> None:
        """Both halves of the facet are refused: an over-length declared name as well as a pattern-illegal one.

        `fit_identifier` shortens only *minted* names; narrowing this check to
        `REASON_PATTERN` would let a successful import yield an ungeneratable project.
        """
        long_port = "p" + "W" * 200
        source = tmp_path / "long_port.arxml"
        source.write_text(
            (FIXTURES_DIR / "minimal_swc.arxml").read_text(encoding="utf-8").replace("pWheelSpeed", long_port),
            encoding="utf-8",
        )
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier") as exc_info:
            run_import(str(source), str(project / "swcraft.yaml"))

        assert REASON_LENGTH in str(exc_info.value)
        assert long_port in str(exc_info.value)
        assert _written_element_files(_model_root(project)) == SCAFFOLDED_ELEMENT_FILES, (
            "a refusal must leave no partial project"
        )

    def test_the_refusal_names_the_string_the_export_declared(self, tmp_path: Path) -> None:
        """An illegal name is quoted as the export declared it, with no rename context."""
        source = tmp_path / "reserved_and_illegal.arxml"
        source.write_text(TWO_GROUPS_ARXML.replace("uint8A", "My Type"), encoding="utf-8")
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier") as exc_info:
            run_import(str(source), str(project / "swcraft.yaml"))

        message = str(exc_info.value)
        # `My Type_impl` is reserved by nothing, so it is not renamed.
        assert "data type 'My Type_impl'" in message, message
        assert "renamed to" not in message, message
        assert "My Type_impl_app" not in message, message

    def test_an_over_length_declared_data_type_is_refused_not_truncated(self, tmp_path: Path) -> None:
        """An over-length declared data type is refused, not imported under a capped name."""
        long_type = "A" + "b" * 140
        source = tmp_path / "long_type.arxml"
        source.write_text(TWO_GROUPS_ARXML.replace("uint8A", long_type), encoding="utf-8")
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier") as exc_info:
            run_import(str(source), str(project / "swcraft.yaml"))

        assert f"data type '{long_type}': {REASON_LENGTH}" in str(exc_info.value), str(exc_info.value)
        assert _written_element_files(_model_root(project)) == SCAFFOLDED_ELEMENT_FILES, (
            "a refusal must leave no partial project"
        )

    def test_a_bare_name_collision_is_also_reported_under_the_declared_name(self, tmp_path: Path) -> None:
        """A bare-name collision renamed by `_reserve()` is reported under the declared name too.

        Cross-kind and in one layer: two declarations of one name in one kind are one
        element or none, and an implementation and an application type of one name keep it both.
        """
        source = tmp_path / "collision_and_illegal.arxml"
        source.write_text(
            TWO_GROUPS_ARXML.replace(_GROUP_A_BASE_TYPE, _APP_PRIMITIVE_OF_THE_CONTESTED_NAME, 1)
            .replace("uint8B_impl", "My Type_impl")
            .replace(_GROUP_B_BASE_TYPE, _APP_RECORD_OF_THE_CONTESTED_NAME, 1),
            encoding="utf-8",
        )
        project = _init_project(tmp_path)

        with pytest.raises(ValueError, match="not a legal AUTOSAR Identifier") as exc_info:
            run_import(str(source), str(project / "swcraft.yaml"))

        message = str(exc_info.value)
        declared = source.read_text(encoding="utf-8")
        quoted = set(re.findall(r"data type '([^']+)'", message))
        assert quoted == {"My Type", "My Type_impl"}, message
        assert all(f"<SHORT-NAME>{name}</SHORT-NAME>" in declared for name in quoted), message
        assert "renamed to 'My Type_app'" in message, message

    def test_a_conforming_export_is_unaffected(self, tmp_path: Path) -> None:
        """The control: the committed fixtures all conform, so the refusal fires only on malformed input."""
        project = _init_project(tmp_path)

        report = run_import(str(FIXTURES_DIR / "minimal_swc.arxml"), str(project / "swcraft.yaml"))

        assert report.written_paths

    def test_a_name_only_a_generated_form_overflows_is_reported_not_silently_shipped(self, tmp_path: Path) -> None:
        """A legal name whose generated form overflows is imported and reported by `check-model`.

        Shortening would rename a declared element; the constraint is SWCraft's, not the standard's.
        """
        model_root, _report = self._long_stub_import(tmp_path)

        issues = check_cross_layer_consistency(model_root=str(model_root)).issues

        assert [issue for issue in issues if INTERNAL_BEHAVIOR_SUFFIX in issue], issues

    def test_the_residual_never_reaches_arxml(self, tmp_path: Path) -> None:
        """The writer refuses the same name before any output file exists, so nothing invalid is written as ARXML."""
        model_root, _report = self._long_stub_import(tmp_path)
        output = tmp_path / "residual.arxml"

        with pytest.raises(ValueError, match="Non-conforming SHORT-NAME"):
            generate_arxml(str(model_root), str(output))

        assert not output.exists(), "a refused generation must leave no partial output"


class TestTheFixturesSameNamePairIsNotRenamed:
    def test_the_report_has_no_rename_section(self, tmp_path: Path) -> None:
        """An application and an implementation type of one name are two name spaces, so neither is renamed."""
        project = _init_project(tmp_path)

        report = run_import(str(FIXTURES_DIR / "data_types.arxml"), str(project / "swcraft.yaml"))

        rendered = report.render()
        assert "Type/prototype/reserved-name renames:" not in rendered
        assert "uint16_impl -> uint16_impl_app" not in rendered
        assert "DoorStateEnum_app" not in rendered
        data_types_dir = _model_root(project) / "data_types"
        assert (data_types_dir / "application" / "DoorStateEnum.yaml").is_file()
        assert (data_types_dir / "implementation" / "DoorStateEnum.yaml").is_file()

    def test_a_dry_run_reports_no_rename_and_writes_nothing(self, tmp_path: Path) -> None:
        """The pass precedes the write, so its decision -- no rename -- is the one a dry run shows."""
        project = _init_project(tmp_path)

        report = run_import(str(FIXTURES_DIR / "data_types.arxml"), str(project / "swcraft.yaml"), dry_run=True)

        assert report.rename_lines == []
        assert report.written_paths == []
        assert _written_element_files(_model_root(project)) == SCAFFOLDED_ELEMENT_FILES


# The init value in the WRITTEN YAML
#
# Asserted on the file's bytes: `interface_to_dict` is hand-written, so a value
# that maps and never reaches a key there is lost with a valid model object left.


class TestInitValuesReachTheWrittenYaml:
    def _imported(self, tmp_path: Path) -> Path:
        project = _init_project(tmp_path, name="imported")
        run_import(str(INIT_VALUES), str(project / "swcraft.yaml"))
        return _model_root(project) / "interfaces"

    def test_the_scalar_shapes_land_in_the_file_with_hex_normalized(self, tmp_path: Path) -> None:
        """Every scalar init shape lands in the written file, with hexadecimal made decimal.

        NUMERICAL-VALUE-SPECIFICATION is the form `[constr_1385]` asks of an
        implementation-typed prototype's own value, APPLICATION-VALUE-SPECIFICATION the
        form `[constr_1221]` asks of an application-typed one. `Counter` is the
        implementation member of a kept same-name pair, so no `Counter_impl` is minted.
        """
        written = (self._imported(tmp_path) / "IScalars.yaml").read_text(encoding="utf-8")
        assert written == (
            "name: IScalars\n"
            "kind: sender_receiver\n"
            "data_elements:\n"
            "- name: counter\n"
            "  implementation_type: Counter\n"
            "  init: 255\n"
            "- name: level\n"
            "  implementation_type: Level\n"
            "  init: 7\n"
            "- name: gear\n"
            "  application_type: Gear\n"
            "  init: DRIVE\n"
            "- name: speed\n"
            "  application_type: Speed\n"
            "  init: 30\n"
            "- name: mode\n"
            "  implementation_type: Mode\n"
            "  init: 'ON'\n"
            "- name: uninitialized\n"
            "  implementation_type: Counter\n"
        )

    def test_the_composite_shapes_land_in_the_file(self, tmp_path: Path) -> None:
        """Array, record and array-of-record init values land in the file, positional throughout."""
        written = (self._imported(tmp_path) / "IComposites.yaml").read_text(encoding="utf-8")
        assert written == (
            "name: IComposites\n"
            "kind: sender_receiver\n"
            "data_elements:\n"
            "- name: triple\n"
            "  application_type: Triple\n"
            "  init:\n"
            "  - 0\n"
            "  - 1\n"
            "  - 16\n"
            "- name: point\n"
            "  application_type: Point\n"
            "  init:\n"
            "    x: 5\n"
            "    y: 10\n"
            "- name: pairs\n"
            "  application_type: PointPair\n"
            "  init:\n"
            "  - x: 1\n"
            "    y: 2\n"
            "  - x: 3\n"
            "    y: 4\n"
        )

    def test_the_written_interface_is_byte_identical_to_the_writers_own_output(self, tmp_path: Path) -> None:
        """`create_interface` and `interface_to_dict` write the same bytes; each builds its dict independently."""
        imported_file = self._imported(tmp_path) / "IScalars.yaml"

        scratch_root = tmp_path / "scratch_model"
        (scratch_root / "interfaces").mkdir(parents=True)
        create_interface(
            str(scratch_root),
            "IScalars",
            [
                {"name": "counter", "implementation_type": "Counter", "init": 255},
                {"name": "level", "implementation_type": "Level", "init": 7},
                {"name": "gear", "application_type": "Gear", "init": "DRIVE"},
                {"name": "speed", "application_type": "Speed", "init": 30},
                {"name": "mode", "implementation_type": "Mode", "init": "ON"},
                {"name": "uninitialized", "implementation_type": "Counter"},
            ],
        )
        assert imported_file.read_bytes() == (scratch_root / "interfaces" / "IScalars.yaml").read_bytes()

    def test_a_described_member_is_byte_identical_through_both_writers(self, tmp_path: Path) -> None:
        """The same pair, over a described data element, operation and argument."""
        from swcraft_tools.arxml_import.report import interface_to_dict
        from swcraft_tools.schema import CSInterface, SRInterface
        from swcraft_tools.writer import create_cs_interface

        elements = [
            {"name": "counter", "implementation_type": "Counter_impl", "init": 255, "description": "Counts the ticks."},
            {"name": "uninitialized", "implementation_type": "Counter_impl"},
        ]
        operations = [
            {
                "name": "Read",
                "arguments": [
                    {
                        "name": "value",
                        "direction": "out",
                        "implementation_type": "uint8",
                        "description": "The value read.",
                    },
                    {"name": "raw", "direction": "out", "implementation_type": "uint8"},
                ],
                "description": "Reads the value.",
            },
            {"name": "Reset", "arguments": []},
        ]
        scratch_root = tmp_path / "scratch_model"
        (scratch_root / "interfaces").mkdir(parents=True)
        create_interface(str(scratch_root), "IDescribed", elements)
        create_cs_interface(str(scratch_root), "IDescribedCs", operations)

        pairs = (
            (SRInterface(name="IDescribed", kind="sender_receiver", data_elements=elements), "IDescribed.yaml", 1),
            (CSInterface(name="IDescribedCs", kind="client_server", operations=operations), "IDescribedCs.yaml", 2),
        )
        for interface, file_name, described in pairs:
            imported = yaml.safe_dump(interface_to_dict(interface), default_flow_style=False, sort_keys=False)
            written = (scratch_root / "interfaces" / file_name).read_text(encoding="utf-8")
            assert imported == written, file_name
            assert written.count("description:") == described, written

    def test_the_behavioural_values_are_byte_identical_through_both_writers(self, tmp_path: Path) -> None:
        """The same pair, over `sw_impl_policy`, `sw_calibration_access` and a service interface of either kind.

        Each is written last and only when set.
        """
        from swcraft_tools.arxml_import.report import interface_to_dict
        from swcraft_tools.schema import CSInterface, SRInterface
        from swcraft_tools.writer import create_cs_interface

        elements = [
            {
                "name": "queued",
                "implementation_type": "Counter_impl",
                "description": "Queued at the receiver.",
                "sw_impl_policy": "queued",
                "sw_calibration_access": "not_accessible",
            },
            {"name": "measured", "implementation_type": "Counter_impl", "sw_calibration_access": "read_only"},
            {"name": "plain", "implementation_type": "Counter_impl"},
        ]
        operations = [{"name": "Reset", "arguments": []}]
        scratch_root = tmp_path / "scratch_model"
        (scratch_root / "interfaces").mkdir(parents=True)
        create_interface(str(scratch_root), "IBehaviour", elements, is_service=True)
        create_cs_interface(str(scratch_root), "IBehaviourCs", operations, is_service=True)
        create_interface(str(scratch_root), "IPlain", [{"name": "plain", "implementation_type": "Counter_impl"}])

        pairs = (
            SRInterface(name="IBehaviour", kind="sender_receiver", data_elements=elements, is_service=True),
            CSInterface(name="IBehaviourCs", kind="client_server", operations=operations, is_service=True),
            SRInterface(
                name="IPlain",
                kind="sender_receiver",
                data_elements=[{"name": "plain", "implementation_type": "Counter_impl"}],
            ),
        )
        for interface in pairs:
            imported = yaml.safe_dump(interface_to_dict(interface), default_flow_style=False, sort_keys=False)
            written = (scratch_root / "interfaces" / f"{interface.name}.yaml").read_text(encoding="utf-8")
            assert imported == written, interface.name
        behaviour = yaml.safe_load((scratch_root / "interfaces" / "IBehaviour.yaml").read_text(encoding="utf-8"))
        assert list(behaviour) == ["name", "kind", "data_elements", "is_service"]
        assert list(behaviour["data_elements"][0]) == [
            "name",
            "implementation_type",
            "description",
            "sw_impl_policy",
            "sw_calibration_access",
        ]
        assert behaviour["data_elements"][1] == {
            "name": "measured",
            "implementation_type": "Counter_impl",
            "sw_calibration_access": "read_only",
        }
        assert behaviour["data_elements"][2] == {"name": "plain", "implementation_type": "Counter_impl"}
        plain = (scratch_root / "interfaces" / "IPlain.yaml").read_text(encoding="utf-8")
        assert "is_service" not in plain and "sw_" not in plain

    def test_a_data_elements_asil_reaches_the_written_yaml(self) -> None:
        """A data element's ASIL reaches the same hand-written dump.

        Unit path only: the writer puts no safety level into ARXML, so no import carries one.
        """
        from swcraft_tools.arxml_import.report import _data_element_to_dict
        from swcraft_tools.schema import DataElement

        rated = _data_element_to_dict(DataElement(name="rated", implementation_type="uint8", asil="D", init=7))
        unrated = _data_element_to_dict(DataElement(name="unrated", implementation_type="uint8"))

        assert rated == {"name": "rated", "implementation_type": "uint8", "init": 7, "asil": "D"}
        assert "asil" not in unrated

    def test_the_imported_project_passes_check_model(self, tmp_path: Path) -> None:
        """The values conform to their declared types, so no `rule:interface.init-*` rule reports."""
        project = _init_project(tmp_path, name="imported")
        run_import(str(INIT_VALUES), str(project / "swcraft.yaml"))
        result = check_cross_layer_consistency(str(_model_root(project)))
        assert [record.message for record in result.records if record.rule.startswith("interface.init-")] == []
        assert result.consistent, result.issues

    def test_the_hexadecimal_rewrite_is_a_report_line_and_not_a_silence(self, tmp_path: Path) -> None:
        """Each hexadecimal spelling made decimal in the YAML gets one report line per element."""
        project = _init_project(tmp_path, name="imported")
        report = run_import(str(INIT_VALUES), str(project / "swcraft.yaml"))

        assert sorted(report.init_radix_lines) == [
            "/Acme/Chassis/Interfaces/IComposites/point: init read from 0x0a, written decimal",
            "/Acme/Chassis/Interfaces/IComposites/triple: init read from 0x10, written decimal",
            "/Acme/Chassis/Interfaces/IScalars/counter: init read from 0xff, written decimal",
        ]

    def test_the_rendered_report_carries_the_radix_section(self, tmp_path: Path) -> None:
        """The line reaches `render()`, the renderer the CLI prints, not only the dataclass."""
        project = _init_project(tmp_path, name="imported")
        rendered = run_import(str(INIT_VALUES), str(project / "swcraft.yaml")).render()

        assert "Init values re-spelled from hexadecimal, octal or binary to decimal (3;" in rendered
        assert "  - /Acme/Chassis/Interfaces/IScalars/counter: init read from 0xff, written decimal" in rendered

    def test_an_import_with_no_hexadecimal_value_renders_no_such_section(self, tmp_path: Path) -> None:
        """No hexadecimal value, no section: an empty heading would claim a loss that did not happen."""
        project = _init_project(tmp_path, name="plain")
        rendered = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml")).render()

        assert "Init values re-spelled" not in rendered

    def _one_port_source(self, tmp_path: Path) -> Path:
        """`init_values.arxml` with the `rComposites` R-PORT removed, so a selection can drop an init interface."""
        source_text = INIT_VALUES.read_text(encoding="utf-8")
        start = source_text.index("                    <R-PORT-PROTOTYPE>")
        end = source_text.index("</R-PORT-PROTOTYPE>\n", start) + len("</R-PORT-PROTOTYPE>\n")
        trimmed = source_text[:start] + source_text[end:]
        assert "rComposites" not in trimmed
        assert "pScalars" in trimmed

        target = tmp_path / "init_values_one_port.arxml"
        target.write_text(trimmed, encoding="utf-8", newline="\n")
        return target

    def test_a_packages_selection_reports_a_radix_line_only_for_what_it_wrote(self, tmp_path: Path) -> None:
        """Every radix line names an element in a written interface file, never one the selection dropped.

        A line is committed against the MAPPED model and the closure runs afterwards,
        so a dropped interface's line would be printed and counted.
        """
        source = self._one_port_source(tmp_path)
        project = _init_project(tmp_path, name="selected")
        model_root = _model_root(project)

        report = run_import(str(source), str(project / "swcraft.yaml"), packages=["Components"])

        assert report.imported_interfaces == 1
        assert any("excluded: IComposites" in line for line in report.closure_lines), report.closure_lines

        written = {
            document["name"]: document
            for document in (
                yaml.safe_load(path.read_text(encoding="utf-8"))
                for path in sorted((model_root / "interfaces").glob("*.yaml"), key=lambda path: path.as_posix())
            )
        }
        assert set(written) == {"IScalars"}

        assert report.init_radix_lines, "the control: the kept interface really does carry a rewrite"
        for line in report.init_radix_lines:
            path = line.split(": init read from", 1)[0]
            interface_name, element_name = path.rsplit("/", 2)[-2:]
            assert interface_name in written, f"{line!r} names an interface in no written file"
            element = next(
                candidate for candidate in written[interface_name]["data_elements"] if candidate["name"] == element_name
            )
            value = element.get("init")
            assert isinstance(value, (int, float)) and not isinstance(value, bool), line

        assert f"({len(report.init_radix_lines)};" in report.render()

    def _calibration_port_source(self, tmp_path: Path) -> Path:
        """`init_values.arxml` with one calibration-parameter port spliced into a copy.

        A `PARAMETER-PROVIDE-COM-SPEC` carries an `INIT-VALUE` plus a `PARAMETER-REF`
        and no `DATA-ELEMENT-REF`. Nothing in the suite XSD-validates this file.
        """
        source_text = INIT_VALUES.read_text(encoding="utf-8")
        anchor = "                  </PORTS>\n"
        assert source_text.count(anchor) == 1, "the fixture's one SWC PORTS block is the splice point"
        port = (
            "                    <P-PORT-PROTOTYPE>\n"
            "                      <SHORT-NAME>pCalibration</SHORT-NAME>\n"
            "                      <PROVIDED-COM-SPECS>\n"
            "                        <PARAMETER-PROVIDE-COM-SPEC>\n"
            "                          <INIT-VALUE>\n"
            "                            <CONSTANT-REFERENCE>\n"
            '                              <CONSTANT-REF DEST="CONSTANT-SPECIFICATION">'
            "/Acme/Chassis/Constants/SR_IScalars_counter_Init</CONSTANT-REF>\n"
            "                            </CONSTANT-REFERENCE>\n"
            "                          </INIT-VALUE>\n"
            '                          <PARAMETER-REF DEST="PARAMETER-DATA-PROTOTYPE">'
            "/Acme/Chassis/Interfaces/ICalibration/gain</PARAMETER-REF>\n"
            "                        </PARAMETER-PROVIDE-COM-SPEC>\n"
            "                      </PROVIDED-COM-SPECS>\n"
            '                      <PROVIDED-INTERFACE-TREF DEST="PARAMETER-INTERFACE">'
            "/Acme/Chassis/Interfaces/ICalibration</PROVIDED-INTERFACE-TREF>\n"
            "                    </P-PORT-PROTOTYPE>\n"
        )
        target = tmp_path / "init_values_calibration.arxml"
        target.write_text(source_text.replace(anchor, port + anchor, 1), encoding="utf-8", newline="\n")
        return target

    def test_a_calibration_parameter_port_is_reported_once_as_its_port(self, tmp_path: Path) -> None:
        """A `PARAMETER-PROVIDE-COM-SPEC` is not read as a malformed sender-receiver one.

        Its port's interface is an external interface stub, so its com-spec is the
        one ``NOT_SUPPORTED`` line a stub port gets, never a "missing DATA-ELEMENT-REF".
        """
        project = _init_project(tmp_path, name="calibrated")
        report = run_import(str(self._calibration_port_source(tmp_path)), str(project / "swcraft.yaml"))

        (line,) = [line for line in report.skip_lines if "pCalibration" in line]
        assert line.startswith("PARAMETER-PROVIDE-COM-SPEC ")
        assert "port on an external interface stub not carried" in line
        assert "/Acme/Chassis/Interfaces/ICalibration" not in {record.target for record in report.unresolved_references}
        (stub,) = [
            stub for stub in report.external_interface_stubs if stub.target == "/Acme/Chassis/Interfaces/ICalibration"
        ]
        assert (stub.name, stub.dest) == ("ICalibration_ext0", "PARAMETER-INTERFACE")
        assert [referrer.path for referrer in stub.referrers] == ["/Acme/Chassis/Components/InitValueSwc/pCalibration"]
        assert [line for line in report.skip_lines if "COM-SPEC" in line] == [line]
        assert [line for line in report.skip_lines if "DATA-ELEMENT-REF" in line] == []

        # The control: the sender-receiver ports beside it are untouched, so the
        # filter drops the parameter kind and not the block.
        written = (_model_root(project) / "interfaces" / "IScalars.yaml").read_text(encoding="utf-8")
        assert "init: 255" in written

    def test_the_import_reports_no_init_skip_for_this_fixture(self, tmp_path: Path) -> None:
        """No init skip: every element that declares an init got one, so no shape is silently refused."""
        project = _init_project(tmp_path, name="imported")
        report = run_import(str(INIT_VALUES), str(project / "swcraft.yaml"))
        assert [line for line in report.skip_lines if "VARIABLE-DATA-PROTOTYPE" in line] == []


class TestOnlyAnEmptyRootCompositionIsReplaced:
    """Only the empty root composition is removed, and its file is found by reading the layer.

    A hand-authored empty placeholder is no root and stays; a root whose filename
    differs from its ``name`` is still found.
    """

    def test_a_hand_authored_empty_placeholder_is_not_removed(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        placeholder = model_root / "compositions" / "Placeholder.yaml"
        placeholder.write_text("name: Placeholder\ntitle: Reserved for the brake subsystem\n", encoding="utf-8")

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert placeholder.is_file(), "an empty NON-root composition is a placeholder, not scaffolding"
        assert not any("Placeholder" in line for line in report.replaced_scaffold_lines)

    def test_a_root_whose_filename_differs_from_its_name_is_still_found(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        scaffolded = model_root / "compositions" / "Root.yaml"
        renamed = model_root / "compositions" / "the-root.yaml"
        renamed.write_text(scaffolded.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        scaffolded.unlink()

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert not renamed.exists(), "the root is located by its declared name, not by its filename"
        assert report.replaced_scaffold_lines == [
            "compositions/the-root.yaml: the empty root composition 'Root' was removed and replaced by the root "
            "this import mapped"
        ]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_a_root_that_cannot_be_located_is_reported_without_a_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root that cannot be located is reported without naming the ``compositions`` directory's path.

        Forced: no tree a user can write reaches it, since the reader and the file scan read one listing.
        """
        from swcraft_tools.arxml_import import orchestrator

        project = _init_project(tmp_path)
        monkeypatch.setattr(orchestrator, "find_element_file", lambda *_args, **_kwargs: None)

        rendered = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml")).render()

        assert (
            "  - the empty root composition 'Root' could not be located in compositions/ and was left in place "
            "-- the project now declares two roots"
        ) in rendered
        for spelling in {str(tmp_path), tmp_path.as_posix()}:
            assert spelling not in rendered

    @pytest.mark.parametrize(
        ("model_root", "target"),
        [
            (
                PurePosixPath("/home/architect/brake/model"),
                PurePosixPath("/home/architect/brake/model/compositions/Root.yaml"),
            ),
            (
                PureWindowsPath(r"C:\Users\architect\brake\model"),
                PureWindowsPath(r"C:\Users\architect\brake\model\compositions\Root.yaml"),
            ),
        ],
        ids=["posix", "windows"],
    )
    def test_the_storage_name_is_spelled_alike_on_both_hosts(self, model_root: PurePath, target: PurePath) -> None:
        """The Windows spelling from any host: the model-root-relative POSIX name, never ``str()`` of a path."""
        from swcraft_tools.arxml_import.orchestrator import _storage_name

        assert _storage_name(model_root, target) == "compositions/Root.yaml"


class TestANarrowedImportReportsOnlyTheReductionsAndRenamesItCarries:
    """A narrowed import reports only the reductions and renames of what its project carries.

    Both are recorded while the export is mapped, before the closure is known, so they are narrowed afterwards.
    """

    SOURCE = Path(__file__).parent / "fixtures" / "arxml_import_shapes" / "packages_selection_report_lines.arxml"
    KEPT_REDUCTION = "/Acme/Kept/AppTypes/Level + /Acme/Kept/Types/Level -> Level (app/impl twin collapse)"
    #: The kept tree's own rename, so a narrowing that dropped every line would be
    #: seen. An application array against an application primitive, one layer.
    KEPT_RENAME = "/Acme/Kept/Arrays/Pressure -> Pressure_app"

    def test_the_whole_export_reports_every_reduction_and_rename(self, tmp_path: Path) -> None:
        """Non-vacuity: the fixture really produces each line the narrowing removes."""
        report = run_import(str(self.SOURCE), str(_init_project(tmp_path) / "swcraft.yaml"))

        assert report.reduction_lines == [
            self.KEPT_REDUCTION,
            "/Acme/Other/AppTypes/Gauge + /Acme/Other/Types/Gauge -> Gauge (app/impl twin collapse)",
        ]
        assert report.rename_lines == [
            self.KEPT_RENAME,
            "/Acme/Other/Arrays/Speed -> Speed_app",
            "LevelSwc -> LevelSwc_ext0 (imported stub, namesake 0 of 1 -- the stub of /Remote/Components/LevelSwc)",
        ]

    def test_a_selection_reports_the_reductions_and_renames_of_what_it_writes(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(self.SOURCE), str(project / "swcraft.yaml"), packages=["Kept"])

        model_root = str(_model_root(project))
        written = sorted(
            (layer, dt.name)
            for layer in ("base_types", "application_types", "implementation_types")
            for dt in read_layer(model_root, layer)
        )
        # The kept tree's implementation `Pressure` is reached by nothing the
        # selection writes, so `Pressure` is written in the application layer only.
        assert written == [
            ("application_types", "Pressure"),
            ("application_types", "Pressure_app"),
            ("base_types", "Word"),
            ("implementation_types", "Level"),
        ]
        assert report.reduction_lines == [self.KEPT_REDUCTION]
        assert report.rename_lines == [self.KEPT_RENAME]
        rendered = report.render()
        assert f"  - {self.KEPT_RENAME}" in rendered
        # The closure lines name what they excluded, `Speed_app` among them; that is not a rename line.
        for name in ("-> Gauge (app/impl", "-> Speed_app", "LevelSwc_ext"):
            assert name not in rendered


class TestAWholeImportReportsTheRenameOfARenamedInterface:
    """The narrowing's per-layer keys, through ``run_import`` and without a selection.

    A wrong interface-layer key would drop every interface rename from an un-narrowed report.
    """

    def test_an_interface_named_like_a_generated_constant_is_renamed_in_the_report(self, tmp_path: Path) -> None:
        basic = Path(__file__).parent / "fixtures" / "arxml_golden" / "basic-4.2.2.arxml"
        text, count = re.subn(
            r"(?<=[>/])IDiagnosticService(?=[<\"/])", "IExample_value_Init", basic.read_text(encoding="utf-8")
        )
        assert count
        source = _source(tmp_path, "constant_collision.arxml", text)

        report = run_import(str(source), str(_init_project(tmp_path) / "swcraft.yaml"))

        assert report.rename_lines == [
            "IExample_value_Init -> IExample_value_Init_app (collides with the CONSTANT-SPECIFICATION "
            "auto-generated for interface 'IExample' data element 'value')"
        ]


class TestTheRenameNarrowingFollowsAnElementNotAName:
    """A rename line is kept when the element it is about is written.

    A data type's line asks its ARXML path (``resolve()`` follows later renames), a
    reserved-name rename the name it gave, and an external stub's line the stub's
    name -- its *old* half was never a name the stub carried.
    """

    def test_a_data_type_line_follows_its_path_through_a_later_rename(self) -> None:
        from swcraft_tools.arxml_import.registry import PathRegistry

        registry = PathRegistry()
        registry.register("/A/Speed", "Speed", "application_types")
        registry.register("/B/Speed", "Speed", "application_types")
        registry.rename("Speed_app", "Speed_app_2", "reserved by the writer", layer="application_types")

        assert registry.carried_renames({"application_types": {"Speed", "Speed_app_2"}}) == registry.renames
        assert registry.carried_renames({"application_types": {"Speed"}}) == []

    def test_the_implementation_type_layer_is_the_one_the_orchestrator_names(self) -> None:
        """The registry spells the layer rather than importing it (it is a base of the package's graph)."""
        from swcraft_tools.arxml_import.merge import LAYER_IMPLEMENTATION_TYPES
        from swcraft_tools.arxml_import.registry import _IMPLEMENTATION_TYPES_LAYER

        assert _IMPLEMENTATION_TYPES_LAYER == LAYER_IMPLEMENTATION_TYPES

    def test_a_reserved_name_rename_is_followed_to_the_name_that_was_written(self) -> None:
        from swcraft_tools.arxml_import.registry import PathRegistry

        registry = PathRegistry()
        registry.record_rename("Brake", "Brake_app", "first reason", layer="swc")
        registry.record_rename("Brake_app", "Brake_app_2", "second reason", layer="swc")

        assert registry.carried_renames({"swc": {"Brake_app_2"}}) == registry.renames
        assert registry.carried_renames({"swc": {"Brake"}}) == []

    def test_a_stub_rename_moves_nothing_it_did_not_carry(self) -> None:
        """The stub never carried ``LevelSwc``, so the kept component that did is not read as the stub."""
        from swcraft_tools.arxml_import.registry import PathRegistry

        registry = PathRegistry()
        registry.record_rename("LevelSwc", "LevelSwc_ext", "claimed", layer="swc", moved=False)
        registry.record_rename("Brake", "Brake_app", "reserved", layer="swc")

        assert registry.carried_renames({"swc": {"LevelSwc", "Brake_app"}}) == ["Brake -> Brake_app (reserved)"]

    def test_a_reduction_is_kept_by_its_application_path(self) -> None:
        from swcraft_tools.arxml_import.registry import PathRegistry

        registry = PathRegistry()
        registry.register("/Impl/Level", "Level", "implementation_types")
        registry.register_twin("/App/Level", "/Impl/Level", "Level")

        # The collapsed application path answers the implementation layer it collapsed onto.
        assert registry.carried_reductions({("implementation_types", "Level")}) == registry.reductions
        assert registry.carried_reductions({("application_types", "Level")}) == []
        assert registry.carried_reductions({("implementation_types", "Other")}) == []


class TestTheUnownedStubPortCountIsNarrowedAndReportedOnItsOwn:
    """Connectors skipped because a stub end names a port its type does not own: counted, narrowed, reported alone.

    A connector naming a stub's own port is kept.
    """

    def _source(self, tmp_path: Path) -> Path:
        return _source(tmp_path, "unowned_stub_port_packages.arxml", UNOWNED_STUB_PORT_PACKAGES_ARXML)

    def test_the_consistent_connector_is_kept_and_counted_nowhere(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        source = _source(tmp_path, "onto_stub_packages.arxml", ONTO_STUB_PACKAGES_ARXML)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert report.unowned_stub_port_paths == []
        assert not [line for line in report.skip_lines if "Assembly_A_to_Ghost" in line]
        assert report.imported_connectors == 1

    def test_a_full_import_reports_the_skip_and_the_stub(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(self._source(tmp_path)), str(project / "swcraft.yaml"))

        assert report.unowned_stub_port_paths == ["/Acme/CompositionsA/CompA/Assembly_A_to_Ghost"]
        assert len(report.external_stub_lines) == 1

    def test_a_selection_that_drops_the_composition_drops_its_skips_too(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(self._source(tmp_path)), str(project / "swcraft.yaml"), packages=["CompositionsB"])

        assert [c for c in report.skip_lines if "Assembly_A_to_Ghost" in c] != [], "the skip line still stands"
        assert report.external_stub_lines == []
        assert report.unowned_stub_port_paths == [], (
            "a count of connectors skipped inside a composition the project never receives is a "
            "claim with nothing behind it"
        )

    def test_the_count_is_rendered_without_a_surviving_stub_line(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(self._source(tmp_path)), str(project / "swcraft.yaml"))

        rendered = dataclasses.replace(report, external_stub_lines=[]).render()

        assert "1 connector(s)/delegation(s)" in rendered, rendered


# A case-only pair of names: two elements, two files, the plain name to the
# element whose source path sorts first


def _case_pair_package(package: str, type_name: str, interface: str, component: str) -> str:
    """One package of :data:`CASE_PAIR_ARXML`: a base type, an implementation type, an interface and a component.

    The data element is typed by the implementation type: no AUTOSAR release admits
    a ``SW-BASE-TYPE`` for a data prototype.
    """
    return f"""\
        <AR-PACKAGE>
          <SHORT-NAME>{package}</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <SW-BASE-TYPE>
                  <SHORT-NAME>{type_name}_Base</SHORT-NAME>
                  <CATEGORY>FIXED_LENGTH</CATEGORY>
                  <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
                  <BASE-TYPE-ENCODING>IEEE754</BASE-TYPE-ENCODING>
                  <NATIVE-DECLARATION>float</NATIVE-DECLARATION>
                </SW-BASE-TYPE>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>{type_name}</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/{package}/Types/{type_name}_Base</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Interfaces</SHORT-NAME>
              <ELEMENTS>
                <SENDER-RECEIVER-INTERFACE>
                  <SHORT-NAME>{interface}</SHORT-NAME>
                  <IS-SERVICE>false</IS-SERVICE>
                  <DATA-ELEMENTS>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>value</SHORT-NAME>
                      <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/{package}/Types/{type_name}</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                  </DATA-ELEMENTS>
                </SENDER-RECEIVER-INTERFACE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>{component}</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pOut</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/{package}/Interfaces/{interface}</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>{component}_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
"""


#: Three case-only pairs (a data type, an interface, a component) laid out so that
#: **only** an order by full source path gives ``float32`` the plain file name:
#: document order and name order would both pick ``Float32``. Both components sit in
#: a package named ``Components``, so they share one group directory and collide.
CASE_PAIR_ARXML = f"""\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
{_case_pair_package("Zeta", "Float32", "ISpeed", "Sensor")}{_case_pair_package("Alpha", "float32", "ispeed", "sensor")}      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: What the pair is written as: model-root-relative file -> the identity it declares.
CASE_PAIR_TREE = {
    "data_types/implementation/float32.yaml": "float32",
    "data_types/implementation/Float32~1.yaml": "Float32",
    "data_types/base/float32_Base.yaml": "float32_Base",
    "data_types/base/Float32_Base~1.yaml": "Float32_Base",
    "interfaces/ispeed.yaml": "ispeed",
    "interfaces/ISpeed~1.yaml": "ISpeed",
    "swc/Components/sensor.yaml": "sensor",
    "swc/Components/Sensor~1.yaml": "Sensor",
}


class TestACaseOnlyPairIsWrittenToTwoFiles:
    """``float32`` beside ``Float32`` is two legal AUTOSAR elements; the import writes both.

    The second gets the nearest free ``~1`` file; the ``name:`` inside, every reference
    and the counts are untouched. Asserted on every host: occupancy is SWCraft's own
    case-insensitive comparison, never the filesystem's answer.
    """

    def _import(self, tmp_path: Path, **kwargs) -> tuple[Path, object]:
        project = _init_project(tmp_path, name=kwargs.pop("name", "proj"))
        source = _source(tmp_path, "case_pair.arxml", CASE_PAIR_ARXML)
        return project, run_import(str(source), str(project / "swcraft.yaml"), **kwargs)

    def test_each_pair_is_two_files_and_the_first_source_path_takes_the_plain_name(self, tmp_path: Path) -> None:
        project, report = self._import(tmp_path)

        # Four data types: each package's implementation type and its base type.
        assert (report.imported_data_types, report.imported_interfaces, report.imported_swcs) == (4, 2, 2)
        assert declared_tree(_model_root(project), CASE_PAIR_TREE) == CASE_PAIR_TREE

    def test_the_written_model_reads_back_both_elements_and_check_model_is_clean(self, tmp_path: Path) -> None:
        project, _report = self._import(tmp_path)
        model_root = _model_root(project)

        assert _imported_types(model_root) == {"float32", "Float32", "float32_Base", "Float32_Base"}
        assert {interface.name for interface in read_interfaces(str(model_root))} == {"ispeed", "ISpeed"}
        assert {swc.name for swc in read_swc_model(str(model_root))} == {"sensor", "Sensor"}
        # Each port still names its own interface -- the reference follows the name, never the file.
        assert {(swc.name, swc.ports[0].interface) for swc in read_swc_model(str(model_root))} == {
            ("sensor", "ispeed"),
            ("Sensor", "ISpeed"),
        }
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_the_report_carries_one_line_per_suffix_naming_both_files(self, tmp_path: Path) -> None:
        _project, report = self._import(tmp_path)

        assert report.file_suffix_lines == [
            "Float32 (/Acme/Zeta/Types/Float32) written as data_types/implementation/Float32~1.yaml -- its plain "
            "file name data_types/implementation/Float32.yaml is already taken, compared case-insensitively; the name and its "
            "references are unchanged",
            "Float32_Base (/Acme/Zeta/Types/Float32_Base) written as data_types/base/Float32_Base~1.yaml -- its "
            "plain file name data_types/base/Float32_Base.yaml is already taken, compared case-insensitively; the "
            "name and its references are unchanged",
            "ISpeed (/Acme/Zeta/Interfaces/ISpeed) written as interfaces/ISpeed~1.yaml -- its plain file name "
            "interfaces/ISpeed.yaml is already taken, compared case-insensitively; the name and its "
            "references are unchanged",
            "Sensor (/Acme/Zeta/Components/Sensor) written as swc/Components/Sensor~1.yaml -- its plain file "
            "name swc/Components/Sensor.yaml is already taken, compared case-insensitively; the name and its "
            "references are unchanged",
        ]
        rendered = report.render()
        header = (
            "Files suffixed for a case-insensitive collision (4; the element names and every reference are unchanged):"
        )
        assert rendered.count(header) == 1, rendered
        for line in report.file_suffix_lines:
            assert f"  - {line}" in rendered

    def test_an_import_that_takes_no_suffix_renders_no_such_section(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)

        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))

        assert report.file_suffix_lines == []
        assert "Files suffixed" not in report.render()
        # The base type declares a NATIVE-DECLARATION, so that section stays empty too.
        assert report.native_declaration_missing_lines == []
        assert "Base types written without a native declaration" not in report.render()

    def test_a_dry_run_lists_the_suffixes_the_import_takes_and_writes_nothing(self, tmp_path: Path) -> None:
        """A dry run lists exactly the suffix lines the real import reports, and writes nothing."""
        dry_project = _init_project(tmp_path, name="dry")
        before = _written_element_files(_model_root(dry_project))
        source = _source(tmp_path, "case_pair.arxml", CASE_PAIR_ARXML)

        dry = run_import(str(source), str(dry_project / "swcraft.yaml"), dry_run=True)

        assert _written_element_files(_model_root(dry_project)) == before
        _project, real = self._import(tmp_path)
        assert len(real.file_suffix_lines) == 4
        assert dry.file_suffix_lines == real.file_suffix_lines
        assert file_suffix_heading(4) in dry.render()

    def test_a_dry_run_over_the_stored_pair_lists_no_suffix(self, tmp_path: Path) -> None:
        """An element the layer already stores is rewritten where it is, in the preview as in the write."""
        project, _first = self._import(tmp_path)

        dry = run_import(str(tmp_path / "case_pair.arxml"), str(project / "swcraft.yaml"), dry_run=True, force=True)

        assert dry.file_suffix_lines == []

    def test_a_file_no_reader_lists_that_already_declares_the_element_is_refused_not_written_into(
        self, tmp_path: Path
    ) -> None:
        """``Float32.YAML`` declares ``Float32`` and matches no ``*.yaml`` glob: the import refuses it.

        Writing there would lose the element from the model while the report listed it written.
        """
        model_root = tmp_path / "model"
        stray = model_root / "data_types" / "implementation" / "Float32.YAML"
        stray.parent.mkdir(parents=True)
        original = "name: Float32\nkind: base_type\nsize: 32\nencoding: IEEE754\n"
        stray.write_text(original, encoding="utf-8", newline="\n")
        suffix_lines: list[str] = []

        with pytest.raises(ValueError, match="'Float32'.*'implementation_types'"):
            imported_file_path(
                model_root,
                "implementation_types",
                "Float32",
                suffix_lines,
                source="/Acme/Types/Float32",
                stored={},
            )

        assert suffix_lines == []
        assert stray.read_text(encoding="utf-8") == original
        assert sorted(path.name for path in stray.parent.iterdir()) == ["Float32.YAML"]

    def test_an_element_already_declared_is_rewritten_in_place_and_only_a_new_one_asks_for_a_free_name(
        self, tmp_path: Path
    ) -> None:
        """``--force`` overwrites the element, found by what it declares -- never a ``~2`` beside its ``~1``.

        A second import of the pair is byte-identical and decides no file name.
        """
        project, _first = self._import(tmp_path)
        model_root = _model_root(project)
        before = {
            path.relative_to(model_root).as_posix(): path.read_bytes()
            for path in sorted(model_root.rglob("*.yaml"), key=lambda p: p.as_posix())
        }

        second = run_import(str(tmp_path / "case_pair.arxml"), str(project / "swcraft.yaml"), force=True)

        after = {
            path.relative_to(model_root).as_posix(): path.read_bytes()
            for path in sorted(model_root.rglob("*.yaml"), key=lambda p: p.as_posix())
        }
        assert after == before
        assert second.file_suffix_lines == [], "no file name was decided: every element was already stored"

    def test_a_forced_import_keeps_the_file_another_element_holds(self, tmp_path: Path) -> None:
        """A hand-authored file under the plain name is another element's, and it survives."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)
        # The held plain name is the incoming base type `float32_Base`'s.
        held = model_element_path(model_root, "float32_Base", kind="base_type")
        held.parent.mkdir(parents=True, exist_ok=True)
        held.write_text(
            yaml.safe_dump({"name": "Other", "kind": "base_type", "size": 8, "encoding": "NONE"}, sort_keys=False),
            encoding="utf-8",
            newline="\n",
        )
        source = _source(tmp_path, "case_pair.arxml", CASE_PAIR_ARXML)

        report = run_import(str(source), str(project / "swcraft.yaml"), force=True)

        assert declared_tree(model_root, ("data_types",)) == {
            "data_types/base/float32_Base.yaml": "Other",
            "data_types/base/float32_Base~1.yaml": "float32_Base",
            "data_types/base/Float32_Base~2.yaml": "Float32_Base",
            "data_types/implementation/float32.yaml": "float32",
            "data_types/implementation/Float32~1.yaml": "Float32",
        }
        # The export's source-path order, across both data description layers.
        assert [line.split(" (", 1)[0] for line in report.file_suffix_lines] == [
            "float32_Base",
            "Float32",
            "Float32_Base",
            "ISpeed",
            "Sensor",
        ]


#: One package, two base types: ``Declared8`` carries a ``NATIVE-DECLARATION``;
#: ``Raw8`` carries none, which every AUTOSAR release permits (``minOccurs="0"``).
NATIVE_DECLARATION_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Types</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Declared8</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <SW-BASE-TYPE>
              <SHORT-NAME>Raw8</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
            </SW-BASE-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


class TestABaseTypeWithoutANativeDeclarationIsListedWrittenReportedAndRefused:
    """A ``SW-BASE-TYPE`` with no ``NATIVE-DECLARATION`` is listed, written, reported and refused.

    It is listed in the report, written without the key, reported by ``check-model`` and refused by generation;
    ``Declared8`` appears in none of these.
    """

    HEADING = "Base types written without a native declaration (check-model reports each)"

    def _import(self, tmp_path: Path) -> tuple[Path, object]:
        project = _init_project(tmp_path)
        source = _source(tmp_path, "native_declaration.arxml", NATIVE_DECLARATION_ARXML)
        return project, run_import(str(source), str(project / "swcraft.yaml"))

    def test_the_report_lists_it_with_its_source_path_and_skips_nothing(self, tmp_path: Path) -> None:
        from swcraft_tools.arxml_import.report import NATIVE_DECLARATION_MISSING_HEADING

        _project, report = self._import(tmp_path)

        assert NATIVE_DECLARATION_MISSING_HEADING == self.HEADING
        assert report.native_declaration_missing_lines == ["Raw8 (/Acme/Types/Raw8)"]
        rendered = report.render()
        assert rendered.count(f"{self.HEADING}:") == 1, rendered
        section = rendered.split(f"{self.HEADING}:", 1)[1].split("\n\n", 1)[0]
        assert section.strip().splitlines() == ["- Raw8 (/Acme/Types/Raw8)"], section
        assert (report.imported_data_types, report.total_skipped) == (2, 0)
        assert "SKIPPED 0 elements" in rendered

    def test_a_dry_run_lists_it_the_same_way_and_writes_nothing(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        source = _source(tmp_path, "native_declaration.arxml", NATIVE_DECLARATION_ARXML)

        report = run_import(str(source), str(project / "swcraft.yaml"), dry_run=True)

        assert report.native_declaration_missing_lines == ["Raw8 (/Acme/Types/Raw8)"]
        assert f"{self.HEADING}:\n  - Raw8 (/Acme/Types/Raw8)" in report.render()
        assert not model_element_path(_model_root(project), "Raw8", kind="base_type").exists()

    def test_the_written_file_carries_no_native_declaration_key(self, tmp_path: Path) -> None:
        project, _report = self._import(tmp_path)
        model_root = _model_root(project)

        raw = yaml.safe_load(model_element_path(model_root, "Raw8", kind="base_type").read_text(encoding="utf-8"))
        declared = yaml.safe_load(
            model_element_path(model_root, "Declared8", kind="base_type").read_text(encoding="utf-8")
        )

        assert "native_declaration" not in raw, raw
        assert declared["native_declaration"] == "uint8"

    def test_check_model_reports_the_rule_and_generation_refuses(self, tmp_path: Path) -> None:
        project, _report = self._import(tmp_path)
        model_root = _model_root(project)

        records = check_cross_layer_consistency(model_root=str(model_root)).records
        assert [(record.rule, record.subjects[0].element) for record in records] == [
            ("data-type.base-type-native-declaration-missing", "Raw8")
        ], records
        output = tmp_path / "out.arxml"
        with pytest.raises(ValueError, match=r"Base type\(s\) \['Raw8'\]"):
            generate_arxml(str(model_root), str(output))
        assert not output.exists()

    def test_a_blank_native_declaration_is_carried_as_written_and_listed(self, tmp_path: Path) -> None:
        """A present but blank ``NATIVE-DECLARATION`` is written as spelled, listed, and reported (the rule reads ``.strip()``)."""
        project = _init_project(tmp_path)
        blank = NATIVE_DECLARATION_ARXML.replace(
            "<BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n            </SW-BASE-TYPE>",
            "<BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n              <NATIVE-DECLARATION>   </NATIVE-DECLARATION>\n"
            "            </SW-BASE-TYPE>",
        )
        assert blank.count("<NATIVE-DECLARATION>") == 2, "precondition: Raw8 now carries a blank element"
        source = _source(tmp_path, "blank_native_declaration.arxml", blank)

        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert report.native_declaration_missing_lines == ["Raw8 (/Acme/Types/Raw8)"]
        written = yaml.safe_load(
            (model_element_path(_model_root(project), "Raw8", kind="base_type")).read_text(encoding="utf-8")
        )
        assert written["native_declaration"] == "   ", written
        records = check_cross_layer_consistency(model_root=str(_model_root(project))).records
        assert [(record.rule, record.subjects[0].element) for record in records] == [
            ("data-type.base-type-native-declaration-missing", "Raw8")
        ], records


def _hierarchy_package(
    package: str, type_name: str, interface: str, sensor: str, gauge: str, body: str, link: str
) -> str:
    """One package of :data:`CASE_PAIR_HIERARCHY_ARXML`: a provider and a consumer wired by one connector in one composition.

    The data element is typed by an implementation type: one typed by the ``SW-BASE-TYPE``
    is skipped with its interface, leaving the connector nothing to wire.
    """
    base = f"/Acme/{package}"
    return f"""\
        <AR-PACKAGE>
          <SHORT-NAME>{package}</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <SW-BASE-TYPE>
                  <SHORT-NAME>{type_name}_Base</SHORT-NAME>
                  <CATEGORY>FIXED_LENGTH</CATEGORY>
                  <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
                  <BASE-TYPE-ENCODING>IEEE754</BASE-TYPE-ENCODING>
                  <NATIVE-DECLARATION>float</NATIVE-DECLARATION>
                </SW-BASE-TYPE>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>{type_name}</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">{base}/Types/{type_name}_Base</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Interfaces</SHORT-NAME>
              <ELEMENTS>
                <SENDER-RECEIVER-INTERFACE>
                  <SHORT-NAME>{interface}</SHORT-NAME>
                  <IS-SERVICE>false</IS-SERVICE>
                  <DATA-ELEMENTS>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>value</SHORT-NAME>
                      <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">{base}/Types/{type_name}</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                  </DATA-ELEMENTS>
                </SENDER-RECEIVER-INTERFACE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>{sensor}</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pOut</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">{base}/Interfaces/{interface}</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>{sensor}_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>{gauge}</SHORT-NAME>
                  <PORTS>
                    <R-PORT-PROTOTYPE>
                      <SHORT-NAME>rIn</SHORT-NAME>
                      <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">{base}/Interfaces/{interface}</REQUIRED-INTERFACE-TREF>
                    </R-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>{gauge}_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Compositions</SHORT-NAME>
              <ELEMENTS>
                <COMPOSITION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>{body}</SHORT-NAME>
                  <COMPONENTS>
                    <SW-COMPONENT-PROTOTYPE>
                      <SHORT-NAME>provider</SHORT-NAME>
                      <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">{base}/Components/{sensor}</TYPE-TREF>
                    </SW-COMPONENT-PROTOTYPE>
                    <SW-COMPONENT-PROTOTYPE>
                      <SHORT-NAME>consumer</SHORT-NAME>
                      <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">{base}/Components/{gauge}</TYPE-TREF>
                    </SW-COMPONENT-PROTOTYPE>
                  </COMPONENTS>
                  <CONNECTORS>
                    <ASSEMBLY-SW-CONNECTOR>
                      <SHORT-NAME>{link}</SHORT-NAME>
                      <PROVIDER-IREF>
                        <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">{base}/Compositions/{body}/provider</CONTEXT-COMPONENT-REF>
                        <TARGET-P-PORT-REF DEST="P-PORT-PROTOTYPE">{base}/Components/{sensor}/pOut</TARGET-P-PORT-REF>
                      </PROVIDER-IREF>
                      <REQUESTER-IREF>
                        <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">{base}/Compositions/{body}/consumer</CONTEXT-COMPONENT-REF>
                        <TARGET-R-PORT-REF DEST="R-PORT-PROTOTYPE">{base}/Components/{gauge}/rIn</TARGET-R-PORT-REF>
                      </REQUESTER-IREF>
                    </ASSEMBLY-SW-CONNECTOR>
                  </CONNECTORS>
                </COMPOSITION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
"""


#: :data:`CASE_PAIR_ARXML`'s layout for **connectors** (``Link`` / ``link``) and
#: **compositions** (``Body`` / ``body``): each pair in a ``Zeta`` package listed
#: first and an ``Alpha`` one second, under one root ``Vehicle``. Only the full
#: source path hands the lower-case element the plain file name.
CASE_PAIR_HIERARCHY_ARXML = f"""\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
{_hierarchy_package("Zeta", "Float32", "ISpeed", "Sensor", "Gauge", "Body", "Link")}\
{_hierarchy_package("Alpha", "float32", "ispeed", "sensor", "gauge", "body", "link")}\
        <AR-PACKAGE>
          <SHORT-NAME>Top</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Vehicle</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>front</SHORT-NAME>
                  <TYPE-TREF DEST="COMPOSITION-SW-COMPONENT-TYPE">/Acme/Zeta/Compositions/Body</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>rear</SHORT-NAME>
                  <TYPE-TREF DEST="COMPOSITION-SW-COMPONENT-TYPE">/Acme/Alpha/Compositions/body</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: The two hierarchy layers as the pair is written: model-root-relative file -> declared identity.
CASE_PAIR_HIERARCHY_TREE = {
    "compositions/Body~1.yaml": "Body",
    "compositions/Vehicle.yaml": "Vehicle",
    "compositions/body.yaml": "body",
    "connectors/Link~1.yaml": "Link",
    "connectors/link.yaml": "link",
}

#: A composition the reserved-name pass renames to ``B_Implementation_app`` (the
#: writer generates ``B_Implementation`` for component ``B``) after the mapper
#: recorded its source path under the old name.
RENAMED_COMPOSITION_ARXML = """\
<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Components</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>B</SHORT-NAME>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>B_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Compositions</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>B_Implementation</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>b</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Components/B</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Top</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>inner</SHORT-NAME>
                  <TYPE-TREF DEST="COMPOSITION-SW-COMPONENT-TYPE">/Acme/Compositions/B_Implementation</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


class TestTheHierarchyLayersAreWrittenInSourcePathOrder:
    """Connectors and compositions take the plain file name by full source path, like the other layers.

    A composition is looked up through the reserved-name rename map, which renames after the paths were recorded.
    """

    def test_a_case_only_connector_pair_and_composition_pair_give_the_lower_source_path_the_plain_name(
        self, tmp_path: Path
    ) -> None:
        project = _init_project(tmp_path)
        source = _source(tmp_path, "case_pair_hierarchy.arxml", CASE_PAIR_HIERARCHY_ARXML)

        dry = run_import(str(source), str(project / "swcraft.yaml"), dry_run=True)
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert (report.imported_connectors, report.imported_compositions) == (2, 3)
        assert declared_tree(_model_root(project), CASE_PAIR_HIERARCHY_TREE) == CASE_PAIR_HIERARCHY_TREE
        suffixed = [line.split(" written as ", 1)[1].split(" -- ", 1)[0] for line in report.file_suffix_lines]
        assert {"connectors/Link~1.yaml", "compositions/Body~1.yaml"} <= set(suffixed)
        assert dry.file_suffix_lines == report.file_suffix_lines

    def test_a_composition_the_reserved_name_pass_renames_is_found_by_its_source_name_and_written(
        self, tmp_path: Path
    ) -> None:
        project = _init_project(tmp_path)
        source = _source(tmp_path, "renamed_composition.arxml", RENAMED_COMPOSITION_ARXML)

        dry = run_import(str(source), str(project / "swcraft.yaml"), dry_run=True)
        report = run_import(str(source), str(project / "swcraft.yaml"))

        assert declared_tree(_model_root(project), ("compositions",)) == {
            "compositions/B_Implementation_app.yaml": "B_Implementation_app",
            "compositions/Top.yaml": "Top",
        }
        assert any("B_Implementation_app" in line for line in report.rename_lines), report.rename_lines
        assert dry.file_suffix_lines == report.file_suffix_lines == []


def _imported_types(model_root: Path) -> set[str]:
    """The names in the three data description layers."""
    return {
        data_type.name
        for data_type in [
            *read_layer(str(model_root), "base_types"),
            *read_layer(str(model_root), "application_types"),
            *read_data_types(str(model_root)),
        ]
    }


class TestTheImportReportNamesNoProductAndGroupsItsSkips:
    """The whole-export import report reads as a generic AUTOSAR import's.

    No line names a modelling tool; the skipped list is one line per distinct reason
    with its count, elements only under ``--verbose``. Does not reach: a reason that
    embeds the element's own name stays one line each (grouping is by the sentence).
    """

    PRODUCT = "rhapsody"

    HEADING = "Skipped elements, one line per reason (--verbose lists each element):"
    VERBOSE_HEADING = "Skipped elements, one line per reason:"

    @staticmethod
    def _skip(tag: str, path: str, reason: str) -> SkipEntry:
        return SkipEntry(path=path, tag=tag, reason=reason, category=SkipCategory.NOT_SUPPORTED, effect="abort")

    def test_the_no_requirement_sentence_carries_no_product_name(self, tmp_path: Path) -> None:
        """The sentence saying no requirement was written is held to the same rule."""
        assert self.PRODUCT not in NO_REQUIREMENT_SENTENCE.casefold()

        project = _init_project(tmp_path)
        run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"))
        assert list((_model_root(project) / "requirements").glob("*.yaml")) == []

    def test_the_header_names_the_source_and_no_product(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(UNSUPPORTED_CONTENT), str(project / "swcraft.yaml"))

        rendered = report.render()
        assert rendered.split("\n", 1)[0] == f"Import report: {UNSUPPORTED_CONTENT}"
        assert self.PRODUCT not in rendered.casefold()
        # The verbose listing adds element paths, the export's own names, not the report's words.
        verbose = [line for line in report.render(verbose=True).split("\n") if not line.startswith("      ")]
        assert self.PRODUCT not in "\n".join(verbose).casefold()

    def test_the_skipped_list_is_one_line_per_reason_with_its_count(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)
        entries = [
            self._skip("OPERATION-INVOKED-EVENT", "/P/A/Beh/E1", "no equivalent"),
            self._skip("SW-ADDR-METHOD", "/P/Addr/M1", "no representation"),
            self._skip("OPERATION-INVOKED-EVENT", "/P/B/Beh/E2", "no equivalent"),
            self._skip("OPERATION-INVOKED-EVENT", "/P/C/Beh/E3", "no equivalent"),
            self._skip("SW-ADDR-METHOD", "/P/Addr/M2", "no representation"),
            self._skip("COMPU-METHOD", "/P/Compu/C1", "no equivalent"),
        ]
        grouped = dataclasses.replace(report, skip_entries=entries)

        lines = grouped.render().split("\n")
        start = lines.index(self.HEADING)
        # Most frequent first; a tie keeps the order the reasons were first met in.
        assert lines[start + 1 : start + 5] == [
            "Not supported by SWCraft (6):",
            "  - 3 × OPERATION-INVOKED-EVENT: no equivalent",
            "  - 2 × SW-ADDR-METHOD: no representation",
            "  - 1 × COMPU-METHOD: no equivalent",
        ]
        assert lines[start + 5] == ""
        assert not any("/P/" in line for line in lines)
        # The per-element lines stay available to every caller that reads them.
        assert grouped.skip_lines[0] == "OPERATION-INVOKED-EVENT /P/A/Beh/E1: no equivalent"
        assert len(grouped.skip_lines) == len(entries)

    def test_a_tie_keeps_the_order_the_reasons_were_first_met_in(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)
        entries = [
            self._skip("SW-ADDR-METHOD", "/P/Addr/M1", "no representation"),
            self._skip("COMPU-METHOD", "/P/Compu/C1", "no equivalent"),
        ]
        lines = dataclasses.replace(report, skip_entries=entries).render().split("\n")
        start = lines.index(self.HEADING)
        assert lines[start + 2 : start + 4] == [
            "  - 1 × SW-ADDR-METHOD: no representation",
            "  - 1 × COMPU-METHOD: no equivalent",
        ]

    def test_a_tie_met_in_sorted_order_keeps_that_order_too(self, tmp_path: Path) -> None:
        """A tie met in ascending order keeps it, so no sort of tag or reason reproduces both tie tests."""
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)
        entries = [
            self._skip("COMPU-METHOD", "/P/Compu/C1", "aa no equivalent"),
            self._skip("SW-ADDR-METHOD", "/P/Addr/M1", "zz no representation"),
            self._skip("COMPU-METHOD", "/P/Compu/C2", "aa no equivalent"),
            self._skip("SW-ADDR-METHOD", "/P/Addr/M2", "zz no representation"),
        ]
        lines = dataclasses.replace(report, skip_entries=entries).render().split("\n")
        start = lines.index(self.HEADING)
        assert lines[start + 2 : start + 4] == [
            "  - 2 × COMPU-METHOD: aa no equivalent",
            "  - 2 × SW-ADDR-METHOD: zz no representation",
        ]

    def test_verbose_lists_every_element_under_its_reason(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)
        entries = [
            self._skip("OPERATION-INVOKED-EVENT", "/P/B/Beh/E2", "no equivalent"),
            self._skip("SW-ADDR-METHOD", "/P/Addr/M1", "no representation"),
            self._skip("OPERATION-INVOKED-EVENT", "/P/A/Beh/E1", "no equivalent"),
        ]
        lines = dataclasses.replace(report, skip_entries=entries).render(verbose=True).split("\n")
        start = lines.index(self.VERBOSE_HEADING)
        assert lines[start + 1 : start + 7] == [
            "Not supported by SWCraft (3):",
            "  - 2 × OPERATION-INVOKED-EVENT: no equivalent",
            "      /P/B/Beh/E2",
            "      /P/A/Beh/E1",
            "  - 1 × SW-ADDR-METHOD: no representation",
            "      /P/Addr/M1",
        ]
        assert self.HEADING not in lines

    def test_a_report_with_no_skips_renders_no_skipped_list(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)
        empty = dataclasses.replace(report, skip_entries=[], skipped_by_tag={})
        assert "Skipped elements" not in empty.render()
        assert "Skipped elements" not in empty.render(verbose=True)

    @staticmethod
    def _argv(project: Path, *extra: str) -> list[str]:
        return [
            "swcraft",
            "import",
            "model",
            "--source",
            str(UNSUPPORTED_CONTENT),
            "--project",
            str(project / "swcraft.yaml"),
            "--dry-run",
            *extra,
        ]

    def test_import_model_verbose_prints_the_elements_and_the_default_does_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(UNSUPPORTED_CONTENT), str(project / "swcraft.yaml"), dry_run=True)
        paths = [entry.path for entry in report.skip_entries]
        assert paths
        capsys.readouterr()

        monkeypatch.setattr("sys.argv", self._argv(project))
        main()
        plain = capsys.readouterr().out
        assert plain.rstrip("\n") == report.render()
        assert not any(f"      {path}" in plain for path in paths)

        report_file = tmp_path / "verbose.txt"
        monkeypatch.setattr("sys.argv", self._argv(project, "--verbose", "--report", str(report_file)))
        main()
        verbose = capsys.readouterr().out
        assert verbose.rstrip("\n") == report.render(verbose=True)
        for path in paths:
            assert f"      {path}" in verbose
        assert report_file.read_text(encoding="utf-8").rstrip("\n") == report.render(verbose=True)

    def test_verbose_is_refused_for_import_component(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = _init_project(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "swcraft",
                "import",
                "component",
                "--source",
                str(UNSUPPORTED_CONTENT),
                "--project",
                str(project / "swcraft.yaml"),
                "--verbose",
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert "--verbose applies only to 'import model'." in capsys.readouterr().err


class TestTheSkippedListIsThreeSections:
    """The skipped list's three sections: export defects, not supported by SWCraft, outside the file.

    Each is printed only when it counts something, decided by ``category``, never by
    text. A defect cites its rule in the release the file declares, or from R4.3.0
    when that release is none SWCraft writes. Does not reach: whether a site chose the
    right category (the entries are hand-built; ``test_skip_categories.py`` holds that).
    """

    HEADING = TestTheImportReportNamesNoProductAndGroupsItsSkips.HEADING
    VERBOSE_HEADING = TestTheImportReportNamesNoProductAndGroupsItsSkips.VERBOSE_HEADING
    DEFECTS_HEADING = "Defects of the export ({count}; the file breaks a rule of the standard, cited{tail}):"
    NOT_SUPPORTED_HEADING = "Not supported by SWCraft ({count}):"
    OUTSIDE_HEADING = "Outside the file ({count}; the file references content it does not declare):"

    @staticmethod
    def _defect(tag: str, path: str, reason: str, rule: StandardRule) -> SkipEntry:
        return SkipEntry(
            path=path, tag=tag, reason=reason, category=SkipCategory.EXPORT_DEFECT, rule=rule, effect="abort"
        )

    @staticmethod
    def _unsupported(tag: str, path: str, reason: str) -> SkipEntry:
        return SkipEntry(path=path, tag=tag, reason=reason, category=SkipCategory.NOT_SUPPORTED, effect="abort")

    @staticmethod
    def _outside(tag: str, path: str, reason: str) -> SkipEntry:
        return SkipEntry(path=path, tag=tag, reason=reason, category=SkipCategory.OUTSIDE_FILE, effect="abort")

    def _entries(self) -> list[SkipEntry]:
        """Interleaved on purpose: the sections do not follow the log's order, the lines within one do."""
        return [
            self._outside("SYNCHRONOUS-SERVER-CALL-POINT", "/P/A/Beh/R/C1", "through a stub"),
            self._unsupported("SYMBOL", "/P/A/Beh/R", "symbol differs"),
            self._defect("ASSEMBLY-SW-CONNECTOR", "/P/Top/C1", "end not owned", StandardRule.INSTANCE_REF_PATH),
            self._defect("DELEGATION-SW-CONNECTOR", "/P/Top/D1", "duplicate link", StandardRule.ONE_CONNECTOR_PER_PAIR),
            self._unsupported("DISPLAY-FORMAT", "/P/T/F", "not read"),
            self._defect("ASSEMBLY-SW-CONNECTOR", "/P/Top/C2", "end not owned", StandardRule.INSTANCE_REF_PATH),
            self._outside("VARIABLE-ACCESS", "/P/A/Beh/R/A1", "port declared nowhere"),
            self._unsupported("DISPLAY-FORMAT", "/P/T/G", "not read"),
        ]

    @staticmethod
    def _report(tmp_path: Path, entries: list[SkipEntry], **changes: object) -> ImportReport:
        """A real report with *entries* as its skipped list, declaring R4.2.2 unless *changes* says otherwise.

        Set here, not inherited from the fixture file, so its ``xsi:schemaLocation`` cannot move the citations.
        """
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)
        skipped_by_tag: dict[str, int] = {}
        for entry in entries:
            skipped_by_tag[entry.tag] = skipped_by_tag.get(entry.tag, 0) + 1
        changes.setdefault("declared_schema", "AUTOSAR_4-2-2.xsd")
        return dataclasses.replace(report, skip_entries=entries, skipped_by_tag=skipped_by_tag, **changes)

    def _section(self, lines: list[str], heading: str) -> list[str]:
        start = lines.index(heading)
        return lines[start : lines.index("", start)]

    def test_the_report_carries_the_release_the_source_declares(self, tmp_path: Path) -> None:
        project = _init_project(tmp_path)
        report = run_import(str(COMPOSITION_NESTED), str(project / "swcraft.yaml"), dry_run=True)
        assert report.declared_schema == "AUTOSAR_4-2-2.xsd"
        assert report.declared_release is ArxmlVersion.V4_2_2

    def test_the_summary_line_splits_its_total_three_ways(self, tmp_path: Path) -> None:
        lines = self._report(tmp_path, self._entries()).render().split("\n")
        assert "SKIPPED 8 elements (export defects 3, not supported by SWCraft 3, outside the file 2)" in lines
        # The per-tag counts stay under it.
        summary = lines.index("SKIPPED 8 elements (export defects 3, not supported by SWCraft 3, outside the file 2)")
        assert lines[summary + 1] == "  ASSEMBLY-SW-CONNECTOR: 2"

    def test_a_zero_count_is_printed_in_the_summary(self, tmp_path: Path) -> None:
        entries = [self._unsupported("SYMBOL", "/P/A/Beh/R", "symbol differs")]
        rendered = self._report(tmp_path, entries).render()
        assert "SKIPPED 1 elements (export defects 0, not supported by SWCraft 1, outside the file 0)" in rendered

    def test_nothing_skipped_is_still_skipped_0_elements_exactly(self, tmp_path: Path) -> None:
        lines = self._report(tmp_path, []).render().split("\n")
        assert "SKIPPED 0 elements" in lines
        assert not any(line.startswith("SKIPPED 0 elements (") for line in lines)
        assert "Skipped elements" not in "\n".join(lines)

    def test_the_three_sections_print_in_order_with_their_lines(self, tmp_path: Path) -> None:
        lines = self._report(tmp_path, self._entries()).render().split("\n")
        start = lines.index(self.HEADING)
        assert lines[start + 1 : lines.index("", start)] == [
            self.DEFECTS_HEADING.format(count=3, tail=""),
            "  - 2 × ASSEMBLY-SW-CONNECTOR (R4.2.2 [TPS_GST_00162]): end not owned",
            "  - 1 × DELEGATION-SW-CONNECTOR (R4.2.2 [constr_1086]): duplicate link",
            self.NOT_SUPPORTED_HEADING.format(count=3),
            "  - 2 × DISPLAY-FORMAT: not read",
            "  - 1 × SYMBOL: symbol differs",
            self.OUTSIDE_HEADING.format(count=2),
            "  - 1 × SYNCHRONOUS-SERVER-CALL-POINT: through a stub",
            "  - 1 × VARIABLE-ACCESS: port declared nowhere",
        ]

    @pytest.mark.parametrize(
        "kept",
        [SkipCategory.EXPORT_DEFECT, SkipCategory.NOT_SUPPORTED, SkipCategory.OUTSIDE_FILE],
    )
    def test_a_section_that_counts_nothing_is_left_out(self, tmp_path: Path, kept: SkipCategory) -> None:
        entries = [entry for entry in self._entries() if entry.category is kept]
        rendered = self._report(tmp_path, entries).render()
        printed = {
            SkipCategory.EXPORT_DEFECT: "Defects of the export (" in rendered,
            SkipCategory.NOT_SUPPORTED: "Not supported by SWCraft (" in rendered,
            SkipCategory.OUTSIDE_FILE: "Outside the file (" in rendered,
        }
        assert printed == {category: category is kept for category in printed}

    def test_the_order_within_a_section_is_most_frequent_first_and_ties_as_met(self, tmp_path: Path) -> None:
        entries = [
            self._defect("DELEGATION-SW-CONNECTOR", "/P/Top/D1", "zz second", StandardRule.DELEGATION_SAME_KIND),
            self._defect("ASSEMBLY-SW-CONNECTOR", "/P/Top/C1", "aa first", StandardRule.CONNECT_PROVIDE_TO_REQUIRE),
            self._defect("VARIABLE-ACCESS", "/P/A/Beh/R/A1", "mm most", StandardRule.SHORT_NAME_UNIQUE),
            self._defect("VARIABLE-ACCESS", "/P/A/Beh/R/A2", "mm most", StandardRule.SHORT_NAME_UNIQUE),
        ]
        lines = self._report(tmp_path, entries).render().split("\n")
        start = lines.index(self.DEFECTS_HEADING.format(count=4, tail=""))
        assert lines[start + 1 : start + 4] == [
            "  - 2 × VARIABLE-ACCESS (R4.2.2 [constr_2508]): mm most",
            "  - 1 × DELEGATION-SW-CONNECTOR (R4.2.2 [constr_1032]): zz second",
            "  - 1 × ASSEMBLY-SW-CONNECTOR (R4.2.2 [TPS_SWCT_01113]): aa first",
        ]

    def test_one_sentence_with_two_rules_is_two_lines(self, tmp_path: Path) -> None:
        """The grouping key is ``(category, rule, tag, reason)``: a line never mixes two rules."""
        entries = [
            self._defect("ASSEMBLY-SW-CONNECTOR", "/P/Top/C1", "same words", StandardRule.CONNECT_PROVIDE_TO_REQUIRE),
            self._defect("ASSEMBLY-SW-CONNECTOR", "/P/Top/C2", "same words", StandardRule.CONNECT_SAME_INTERFACE_KIND),
        ]
        section = self._section(
            self._report(tmp_path, entries).render().split("\n"), self.DEFECTS_HEADING.format(count=2, tail="")
        )
        assert section[1:] == [
            "  - 1 × ASSEMBLY-SW-CONNECTOR (R4.2.2 [TPS_SWCT_01113]): same words",
            "  - 1 × ASSEMBLY-SW-CONNECTOR (R4.2.2 [constr_1036]): same words",
        ]

    @pytest.mark.parametrize(
        ("declared_schema", "release"),
        [("AUTOSAR_4-2-2.xsd", "4.2.2"), ("AUTOSAR_4-3-0.xsd", "4.3.0"), ("autosar_4-3-0.XSD", "4.3.0")],
    )
    def test_a_citation_names_the_release_the_file_declares(
        self, tmp_path: Path, declared_schema: str, release: str
    ) -> None:
        rendered = self._report(tmp_path, self._entries(), declared_schema=declared_schema).render()
        assert f"  - 2 × ASSEMBLY-SW-CONNECTOR (R{release} [TPS_GST_00162]): end not owned" in rendered
        assert self.DEFECTS_HEADING.format(count=3, tail="") in rendered, "no fallback clause for a known release"

    @pytest.mark.parametrize(
        ("declared_schema", "tail"),
        [
            ("AUTOSAR_00051.xsd", "; cited from R4.3.0 — the file declares AUTOSAR_00051.xsd"),
            (None, "; cited from R4.3.0 — the file declares no schema"),
        ],
    )
    def test_a_file_declaring_no_release_swcraft_writes_is_cited_from_the_fallback(
        self, tmp_path: Path, declared_schema: str | None, tail: str
    ) -> None:
        lines = self._report(tmp_path, self._entries(), declared_schema=declared_schema).render().split("\n")
        start = lines.index(self.DEFECTS_HEADING.format(count=3, tail=tail))
        assert lines[start + 1] == "  - 2 × ASSEMBLY-SW-CONNECTOR (R4.3.0 [TPS_GST_00162]): end not owned"

    def test_verbose_lists_the_elements_under_each_sections_lines(self, tmp_path: Path) -> None:
        report = self._report(tmp_path, self._entries())
        lines = report.render(verbose=True).split("\n")
        start = lines.index(self.VERBOSE_HEADING)
        assert lines[start + 1 : lines.index("", start)] == [
            self.DEFECTS_HEADING.format(count=3, tail=""),
            "  - 2 × ASSEMBLY-SW-CONNECTOR (R4.2.2 [TPS_GST_00162]): end not owned",
            "      /P/Top/C1",
            "      /P/Top/C2",
            "  - 1 × DELEGATION-SW-CONNECTOR (R4.2.2 [constr_1086]): duplicate link",
            "      /P/Top/D1",
            self.NOT_SUPPORTED_HEADING.format(count=3),
            "  - 2 × DISPLAY-FORMAT: not read",
            "      /P/T/F",
            "      /P/T/G",
            "  - 1 × SYMBOL: symbol differs",
            "      /P/A/Beh/R",
            self.OUTSIDE_HEADING.format(count=2),
            "  - 1 × SYNCHRONOUS-SERVER-CALL-POINT: through a stub",
            "      /P/A/Beh/R/C1",
            "  - 1 × VARIABLE-ACCESS: port declared nowhere",
            "      /P/A/Beh/R/A1",
        ]
        assert not any(line.startswith("      /") for line in report.render().split("\n"))

    @pytest.mark.parametrize(
        ("declared_schema", "citation"),
        [
            ("AUTOSAR_4-3-0.xsd", "R4.3.0 [TPS_GST_00162]"),
            (None, "R4.3.0 [TPS_GST_00162]"),
            ("AUTOSAR_4-2-2.xsd", "R4.2.2 [TPS_GST_00162]"),
        ],
    )
    def test_the_stub_blocks_count_line_cites_the_files_release(
        self, tmp_path: Path, declared_schema: str | None, citation: str
    ) -> None:
        report = self._report(
            tmp_path,
            self._entries(),
            declared_schema=declared_schema,
            unowned_stub_port_paths=["/P/Top/C1", "/P/Top/C2"],
        )
        rendered = report.render()
        assert (
            "2 connector(s)/delegation(s) reaching an external stub were skipped because the target port path "
            f"is not owned by the context component's type ({citation}) -- a defect of the export; "
            'see "Defects of the export" below.'
        ) in rendered
        assert "constr_2626" not in rendered
        assert "R22-11" not in rendered


# The base type's three comparison-only properties stop at the reader


class TestTheComparisonOnlyBaseTypePropertiesReachNoModelElement:
    """``BYTE-ORDER``, ``MEM-ALIGNMENT`` and ``CATEGORY`` stop at the reader: an import is unchanged by them.

    The merge verdict needs them; SWCraft's ``base_type`` has no field for any.
    Does not reach: a second declaration of the name, the component importer, the written ARXML.
    """

    _SOURCE = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<AUTOSAR xmlns="http://autosar.org/schema/r4.0">\n'
        "  <AR-PACKAGES>\n"
        "    <AR-PACKAGE>\n"
        "      <SHORT-NAME>Pkg</SHORT-NAME>\n"
        "      <ELEMENTS>\n"
        "        <SW-BASE-TYPE>\n"
        "          <SHORT-NAME>Declared</SHORT-NAME>\n"
        "          <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
        "          <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>\n"
        "          <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
        "          <MEM-ALIGNMENT>8</MEM-ALIGNMENT>\n"
        "          <BYTE-ORDER>MOST-SIGNIFICANT-BYTE-FIRST</BYTE-ORDER>\n"
        "          <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>\n"
        "        </SW-BASE-TYPE>\n"
        "        <SW-BASE-TYPE>\n"
        "          <SHORT-NAME>Silent</SHORT-NAME>\n"
        "          <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>\n"
        "          <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
        "          <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>\n"
        "        </SW-BASE-TYPE>\n"
        "      </ELEMENTS>\n"
        "    </AR-PACKAGE>\n"
        "  </AR-PACKAGES>\n"
        "</AUTOSAR>\n"
    )

    @pytest.fixture
    def imported(self, tmp_path: Path) -> Path:
        source = tmp_path / "comparison_only.arxml"
        source.write_text(self._SOURCE, encoding="utf-8", newline="\n")
        project = _init_project(tmp_path)
        report = run_import(str(source), str(project / "swcraft.yaml"))
        assert report.imported_data_types == 2
        return project

    def test_the_reader_did_carry_all_three_off_this_very_export(self, tmp_path: Path) -> None:
        """The reader carries all three off this export, so the absences asserted below are not vacuous."""
        source = tmp_path / "carried.arxml"
        source.write_text(self._SOURCE, encoding="utf-8", newline="\n")
        declared = {info.name: info for info in read_arxml(str(source)).sw_base_types}["Declared"]
        assert (declared.byte_order, declared.mem_alignment, declared.category) == (
            "MOST-SIGNIFICANT-BYTE-FIRST",
            "8",
            "FIXED_LENGTH",
        )

    def test_neither_yaml_file_carries_any_of_the_three(self, imported: Path) -> None:
        for name in ("Declared", "Silent"):
            document = model_element_path(_model_root(imported), name, kind="base_type")
            parsed = yaml.safe_load(document.read_text(encoding="utf-8"))
            assert set(parsed) == {"name", "kind", "size", "encoding", "native_declaration"}

    def test_the_declaring_base_type_is_written_exactly_as_the_silent_one(self, imported: Path) -> None:
        """The three properties change nothing: the two files differ in their ``name`` alone."""
        written = {
            name: (model_element_path(_model_root(imported), name, kind="base_type")).read_text(encoding="utf-8")
            for name in ("Declared", "Silent")
        }
        assert written["Declared"].replace("Declared", "Silent") == written["Silent"]

    def test_the_loaded_elements_carry_no_such_field(self, imported: Path) -> None:
        """Read back through the model's own reader, not off the file, so a field held only in memory is seen."""
        loaded = {dt.name: dt for dt in read_layer(str(_model_root(imported)), "base_types")}
        assert set(loaded) == {"Declared", "Silent"}
        for element in loaded.values():
            assert not {"byte_order", "mem_alignment", "category"} & set(type(element).model_fields)
            assert element.model_dump(exclude_defaults=True) == {
                "name": element.name,
                "kind": "base_type",
                "size": 8,
                "encoding": "NONE",
                "native_declaration": "uint8",
            }


class TestTheThreeCategoriesOfTheVerdictAndTheReferences:
    """The report's merge verdict and reference categories, at the CLI.

    Merged, conflicting and unresolved each have a count in the summary block,
    printed only when it counts something; ``--dry-run`` prints all three alike. No
    line of the three carries a filesystem path (the header names the export passed;
    an ARXML path is the export's own text). Does not reach: the ``outside the
    selected packages`` sentence (``test_arxml_import_unresolved.py`` renders it).
    """

    SHAPES_DIR = Path(__file__).parent / "fixtures" / "arxml_import_shapes"
    MERGES = SHAPES_DIR / "identical_duplicates.arxml"
    #: A conflicting pair, plus the one reference into it -- two categories in one file.
    CONFLICT = SHAPES_DIR / "packages_selection_conflict.arxml"
    ROLES = SHAPES_DIR / "unresolved_reference_roles.arxml"

    HEADINGS = ("Merged declarations", "Conflicting declarations", "Unresolved references")

    @classmethod
    def _sections(cls, rendered: str) -> dict[str, list[str]]:
        """Every one of the three categories present, heading line included."""
        lines = rendered.split("\n")
        sections: dict[str, list[str]] = {}
        for index, line in enumerate(lines):
            for heading in cls.HEADINGS:
                if line.startswith(heading):
                    end = lines.index("", index) if "" in lines[index:] else len(lines)
                    sections[heading] = lines[index:end]
        return sections

    def test_the_three_counts_join_the_summary(self, tmp_path: Path) -> None:
        merged = run_import(str(self.MERGES), str(_init_project(tmp_path, "a") / "swcraft.yaml"), dry_run=True)
        conflicting = run_import(str(self.CONFLICT), str(_init_project(tmp_path, "b") / "swcraft.yaml"), dry_run=True)
        roles = run_import(str(self.ROLES), str(_init_project(tmp_path, "c") / "swcraft.yaml"), dry_run=True)

        assert "MERGED 2 duplicate name group(s)" in merged.render().split("\n")
        assert "CONFLICTING 2 duplicate name group(s)" in conflicting.render().split("\n")
        # Three of the six absent targets are external stubs; the other three are in
        # roles no stub may take (a value type's base, a record field's and an array element's type).
        assert "UNRESOLVED 3 reference(s) to 3 target(s)" in roles.render().split("\n")
        # Each count is above every category it counts, in the summary block.
        summary = conflicting.render().split("\n")
        assert summary.index("CONFLICTING 2 duplicate name group(s)") < summary.index(
            self._sections(conflicting.render())["Conflicting declarations"][0]
        )

    def test_a_count_is_printed_only_when_it_counts_something(self, tmp_path: Path) -> None:
        """An export that declares every name once and dangles no reference prints none of the three."""
        report = run_import(str(COMPOSITION_NESTED), str(_init_project(tmp_path) / "swcraft.yaml"), dry_run=True)

        rendered = report.render()

        assert (report.merged_groups, report.conflicting_groups, report.unresolved_references) == ([], [], [])
        for word in ("MERGED ", "CONFLICTING ", "UNRESOLVED ", *self.HEADINGS):
            assert word not in rendered

    @pytest.mark.parametrize("source", ["MERGES", "CONFLICT", "ROLES"])
    def test_the_dry_run_prints_the_three_categories_identically(self, tmp_path: Path, source: str) -> None:
        """The three categories are decided before a file name is allocated, so a preview prints them alike."""
        export = str(getattr(self, source))
        preview = run_import(export, str(_init_project(tmp_path, "preview") / "swcraft.yaml"), dry_run=True)
        real = run_import(export, str(_init_project(tmp_path, "real") / "swcraft.yaml"))

        assert self._sections(preview.render()) == self._sections(real.render())
        assert self._sections(preview.render(verbose=True)) == self._sections(real.render(verbose=True))
        # Non-vacuous: the fixture really does render at least one of the three.
        assert self._sections(preview.render())
        # A preview removes nothing, so the removal category is empty on it.
        assert preview.replaced_scaffold_lines == []

    @pytest.mark.parametrize("source", ["MERGES", "CONFLICT", "ROLES"])
    def test_no_line_of_the_three_categories_names_a_place_on_this_machine(self, tmp_path: Path, source: str) -> None:
        export = Path(getattr(self, source))
        project = _init_project(tmp_path)
        report = run_import(str(export), str(project / "swcraft.yaml"))

        rendered = report.render(verbose=True)
        assert rendered.split("\n")[0] == f"Import report: {export}"  # the stated exception
        for heading, section in self._sections(rendered).items():
            body = "\n".join(section)
            for place in (str(tmp_path), str(export), export.name, str(project), str(_model_root(project))):
                assert place not in body, f"{heading} names {place}"
            for line in section:
                assert "\\" not in line, line


# `--packages` after the verdict

#: Two parallel copies of one type cluster; the App package's interface names
#: **both**, the shape a one-tree selection leaves a reference in. `Word` differs
#: between the trees, so both groups conflict over the whole file. Invented names only.
SELECTION_EXCLUDED_TARGET_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>TreeA</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <SW-BASE-TYPE>
                  <SHORT-NAME>Word</SHORT-NAME>
                  <CATEGORY>FIXED_LENGTH</CATEGORY>
                  <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
                  <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
                  <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
                </SW-BASE-TYPE>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Ratio</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/TreeA/Types/Word</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeB</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <SW-BASE-TYPE>
                  <SHORT-NAME>Word</SHORT-NAME>
                  <CATEGORY>FIXED_LENGTH</CATEGORY>
                  <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
                  <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
                  <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
                </SW-BASE-TYPE>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Ratio</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/TreeB/Types/Word</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>App</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Interfaces</SHORT-NAME>
              <ELEMENTS>
                <SENDER-RECEIVER-INTERFACE>
                  <SHORT-NAME>IRatio</SHORT-NAME>
                  <IS-SERVICE>false</IS-SERVICE>
                  <DATA-ELEMENTS>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>ratioA</SHORT-NAME>
                      <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeA/Types/Ratio</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>ratioB</SHORT-NAME>
                      <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeB/Types/Ratio</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                  </DATA-ELEMENTS>
                </SENDER-RECEIVER-INTERFACE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>RatioSwc</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pRatio</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/App/Interfaces/IRatio</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>RatioSwc_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: Two independent groups; the second dangles a reference the export does not
#: declare, so a selection of the first carries nothing of it.
SELECTION_NARROWED_REPORT_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>GroupA</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>uint8A</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>LevelA</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/GroupA/uint8A</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>IGroupA</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>level</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/GroupA/LevelA</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>SwcA</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pA</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/GroupA/IGroupA</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>SwcA_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>GroupB</SHORT-NAME>
          <ELEMENTS>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>IGroupB</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>level</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Elsewhere/LevelB</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>SwcB</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pB</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/GroupB/IGroupB</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>SwcB_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: Two compositions in parallel packages, each declaring an assembly connector of
#: the same SHORT-NAME. ``connectors/`` is flat, so the group's first equality
#: property is the owning composition and the pair conflicts by construction; a
#: one-package selection keeps one member. Invented names only.
SELECTION_CONNECTOR_CONFLICT_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Common</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Word</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Level</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Common/Word</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ILevel</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>level</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Common/Level</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Producer</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pLevel</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Common/ILevel</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>Producer_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Consumer</SHORT-NAME>
              <PORTS>
                <R-PORT-PROTOTYPE>
                  <SHORT-NAME>rLevel</SHORT-NAME>
                  <REQUIRED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Common/ILevel</REQUIRED-INTERFACE-TREF>
                </R-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>Consumer_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeA</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Top</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>cellA</SHORT-NAME>
                  <TYPE-TREF DEST="COMPOSITION-SW-COMPONENT-TYPE">/Acme/TreeA/Cell</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>cellB</SHORT-NAME>
                  <TYPE-TREF DEST="COMPOSITION-SW-COMPONENT-TYPE">/Acme/TreeB/Cell2</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Cell</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>producer</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Common/Producer</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>consumer</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Common/Consumer</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
              <CONNECTORS>
                <ASSEMBLY-SW-CONNECTOR>
                  <SHORT-NAME>Wire</SHORT-NAME>
                  <PROVIDER-IREF>
                    <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">/Acme/TreeA/Cell/producer</CONTEXT-COMPONENT-REF>
                    <TARGET-P-PORT-REF DEST="P-PORT-PROTOTYPE">/Acme/Common/Producer/pLevel</TARGET-P-PORT-REF>
                  </PROVIDER-IREF>
                  <REQUESTER-IREF>
                    <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">/Acme/TreeA/Cell/consumer</CONTEXT-COMPONENT-REF>
                    <TARGET-R-PORT-REF DEST="R-PORT-PROTOTYPE">/Acme/Common/Consumer/rLevel</TARGET-R-PORT-REF>
                  </REQUESTER-IREF>
                </ASSEMBLY-SW-CONNECTOR>
              </CONNECTORS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeB</SHORT-NAME>
          <ELEMENTS>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Cell2</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>producer</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Common/Producer</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>consumer</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/Common/Consumer</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
              <CONNECTORS>
                <ASSEMBLY-SW-CONNECTOR>
                  <SHORT-NAME>Wire</SHORT-NAME>
                  <PROVIDER-IREF>
                    <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">/Acme/TreeB/Cell2/producer</CONTEXT-COMPONENT-REF>
                    <TARGET-P-PORT-REF DEST="P-PORT-PROTOTYPE">/Acme/Common/Producer/pLevel</TARGET-P-PORT-REF>
                  </PROVIDER-IREF>
                  <REQUESTER-IREF>
                    <CONTEXT-COMPONENT-REF DEST="SW-COMPONENT-PROTOTYPE">/Acme/TreeB/Cell2/consumer</CONTEXT-COMPONENT-REF>
                    <TARGET-R-PORT-REF DEST="R-PORT-PROTOTYPE">/Acme/Common/Consumer/rLevel</TARGET-R-PORT-REF>
                  </REQUESTER-IREF>
                </ASSEMBLY-SW-CONNECTOR>
              </CONNECTORS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: An application primitive is imported as a step onto the implementation type its
#: ``DATA-TYPE-MAPPING-SET`` entry names, so the closure walk must follow that entry.
#: The implementation type is declared in two parallel trees that differ under it,
#: and only the mapping-set entry says which one the selection needs. Invented names only.
SELECTION_MAPPING_SET_REACH_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Left</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Word</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Raw</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Left/Word</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Right</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Word</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Raw</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Right/Word</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>App</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
            <DATA-TYPE-MAPPING-SET>
              <SHORT-NAME>Maps</SHORT-NAME>
              <DATA-TYPE-MAPS>
                <DATA-TYPE-MAP>
                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</APPLICATION-DATA-TYPE-REF>
                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Left/Raw</IMPLEMENTATION-DATA-TYPE-REF>
                </DATA-TYPE-MAP>
              </DATA-TYPE-MAPS>
            </DATA-TYPE-MAPPING-SET>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ISpeed</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>speed</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Sensor</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pSpeed</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/App/ISpeed</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>Sensor_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Root</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>sensor</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/App/Sensor</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: ``--packages App,TreeA`` EXCLUDES the singleton primitive's twin entry (it names
#: ``TreeB``'s member of a conflicting group), so the mapping layer falls back to the
#: generic entry, which names a member of a *second* conflicting group only this walk
#: can re-admit. Invented names only.
SELECTION_MAPPING_SET_FALL_BACK_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>TreeA</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>WordA</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/TreeA/WordA</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeB</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>WordB</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint32</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/TreeB/WordB</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>GenLeft</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Octet</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Raw</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/GenLeft/Octet</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>GenRight</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Quad</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint32</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Raw</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/GenRight/Quad</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>App</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
            <DATA-TYPE-MAPPING-SET>
              <SHORT-NAME>Maps</SHORT-NAME>
              <DATA-TYPE-MAPS>
                <DATA-TYPE-MAP>
                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</APPLICATION-DATA-TYPE-REF>
                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/GenLeft/Raw</IMPLEMENTATION-DATA-TYPE-REF>
                </DATA-TYPE-MAP>
                <DATA-TYPE-MAP>
                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</APPLICATION-DATA-TYPE-REF>
                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeB/Speed</IMPLEMENTATION-DATA-TYPE-REF>
                </DATA-TYPE-MAP>
              </DATA-TYPE-MAPS>
            </DATA-TYPE-MAPPING-SET>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ISpeed</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>speed</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Sensor</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pSpeed</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/App/ISpeed</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>Sensor_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Root</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>sensor</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/App/Sensor</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: The same shape with both mapping entries naming members of **one** group, which
#: ``--packages App`` names neither of: reaching both members leaves it conflicting.
#: Invented names only.
SELECTION_MAPPING_SET_FALL_BACK_IN_ONE_GROUP_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>TreeA</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>WordA</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/TreeA/WordA</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeB</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>WordB</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint32</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/TreeB/WordB</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>App</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
            <DATA-TYPE-MAPPING-SET>
              <SHORT-NAME>Maps</SHORT-NAME>
              <DATA-TYPE-MAPS>
                <DATA-TYPE-MAP>
                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</APPLICATION-DATA-TYPE-REF>
                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeA/Speed</IMPLEMENTATION-DATA-TYPE-REF>
                </DATA-TYPE-MAP>
                <DATA-TYPE-MAP>
                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</APPLICATION-DATA-TYPE-REF>
                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeB/Speed</IMPLEMENTATION-DATA-TYPE-REF>
                </DATA-TYPE-MAP>
              </DATA-TYPE-MAPS>
            </DATA-TYPE-MAPPING-SET>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ISpeed</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>speed</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Sensor</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pSpeed</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/App/ISpeed</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>Sensor_InternalBehavior</SHORT-NAME>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                  <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Root</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>sensor</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/App/Sensor</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: The one-group shape over a **second** conflicting group: each member of the
#: twin's group rests on its own declaration of one base-type ``SHORT-NAME``, so the
#: member the walk follows decides the base type re-admitted. ``--packages App``
#: names no member (the walk narrows it); ``--packages App,TreeA`` names one
#: (``narrow_to_selection`` keeps it by package). Invented names only.
SELECTION_MAPPING_SET_ONE_GROUP_OVER_ONE_BASE_GROUP_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>BaseA</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Word</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>BaseB</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Word</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint32</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeA</SHORT-NAME>
          <ELEMENTS>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/BaseA/Word</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeB</SHORT-NAME>
          <ELEMENTS>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/BaseB/Word</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>App</SHORT-NAME>
          <ELEMENTS>
            <APPLICATION-PRIMITIVE-DATA-TYPE>
              <SHORT-NAME>Speed</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
            </APPLICATION-PRIMITIVE-DATA-TYPE>
            <DATA-TYPE-MAPPING-SET>
              <SHORT-NAME>Maps</SHORT-NAME>
              <DATA-TYPE-MAPS>
                <DATA-TYPE-MAP>
                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</APPLICATION-DATA-TYPE-REF>
                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeA/Speed</IMPLEMENTATION-DATA-TYPE-REF>
                </DATA-TYPE-MAP>
                <DATA-TYPE-MAP>
                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</APPLICATION-DATA-TYPE-REF>
                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeB/Speed</IMPLEMENTATION-DATA-TYPE-REF>
                </DATA-TYPE-MAP>
              </DATA-TYPE-MAPS>
            </DATA-TYPE-MAPPING-SET>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ISpeed</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>speed</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Speed</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
            <APPLICATION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Sensor</SHORT-NAME>
              <PORTS>
                <P-PORT-PROTOTYPE>
                  <SHORT-NAME>pSpeed</SHORT-NAME>
                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/App/ISpeed</PROVIDED-INTERFACE-TREF>
                </P-PORT-PROTOTYPE>
              </PORTS>
              <INTERNAL-BEHAVIORS>
                <SWC-INTERNAL-BEHAVIOR>
                  <SHORT-NAME>SensorBehavior</SHORT-NAME>
                  <DATA-TYPE-MAPPING-REFS>
                    <DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Acme/App/Maps</DATA-TYPE-MAPPING-REF>
                  </DATA-TYPE-MAPPING-REFS>
                  <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                </SWC-INTERNAL-BEHAVIOR>
              </INTERNAL-BEHAVIORS>
            </APPLICATION-SW-COMPONENT-TYPE>
            <COMPOSITION-SW-COMPONENT-TYPE>
              <SHORT-NAME>Root</SHORT-NAME>
              <COMPONENTS>
                <SW-COMPONENT-PROTOTYPE>
                  <SHORT-NAME>sensor</SHORT-NAME>
                  <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/App/Sensor</TYPE-TREF>
                </SW-COMPONENT-PROTOTYPE>
              </COMPONENTS>
            </COMPOSITION-SW-COMPONENT-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: A **merged** one-group row both of whose members the selection names, over a
#: conflicting ``Speed`` group one **singleton** (``/Acme/Shared/Types/Level``)
#: further down. Neither ``Speed`` is in a selected package, so only the walk's
#: *reached* can re-admit one. Invented names only.
SELECTION_MERGED_ONE_GROUP_OVER_A_SINGLETON_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Bits</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Uint16</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <SW-BASE-TYPE>
              <SHORT-NAME>Uint32</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>32</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint32</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeX</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Speed</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Bits/Uint16</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeY</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Speed</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Bits/Uint32</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Shared</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Level</SHORT-NAME>
                  <CATEGORY>TYPE_REFERENCE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeX/Types/Speed</IMPLEMENTATION-DATA-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>Selected</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>TreeA</SHORT-NAME>
              <ELEMENTS>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Word</SHORT-NAME>
                  <CATEGORY>TYPE_REFERENCE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Shared/Types/Level</IMPLEMENTATION-DATA-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>TreeB</SHORT-NAME>
              <ELEMENTS>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Word</SHORT-NAME>
                  <CATEGORY>TYPE_REFERENCE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Shared/Types/Level</IMPLEMENTATION-DATA-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>AppTypes</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-PRIMITIVE-DATA-TYPE>
                  <SHORT-NAME>Word</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                </APPLICATION-PRIMITIVE-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Maps</SHORT-NAME>
              <ELEMENTS>
                <DATA-TYPE-MAPPING-SET>
                  <SHORT-NAME>Set</SHORT-NAME>
                  <DATA-TYPE-MAPS>
                    <DATA-TYPE-MAP>
                      <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/Selected/AppTypes/Word</APPLICATION-DATA-TYPE-REF>
                      <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Selected/TreeA/Word</IMPLEMENTATION-DATA-TYPE-REF>
                    </DATA-TYPE-MAP>
                  </DATA-TYPE-MAPS>
                </DATA-TYPE-MAPPING-SET>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Interfaces</SHORT-NAME>
              <ELEMENTS>
                <SENDER-RECEIVER-INTERFACE>
                  <SHORT-NAME>IWord</SHORT-NAME>
                  <IS-SERVICE>false</IS-SERVICE>
                  <DATA-ELEMENTS>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>value</SHORT-NAME>
                      <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/Selected/AppTypes/Word</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                  </DATA-ELEMENTS>
                </SENDER-RECEIVER-INTERFACE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>Sensor</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pWord</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Selected/Interfaces/IWord</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>Sensor_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: A **conflicting** one-group row the selection names no member of, whose **twin**
#: registers nothing: the mapping set's generic entry is ``TreeA``'s ``Ratio``, its
#: twin ``TreeB``'s, which rests on a ``SW-BASE-TYPE`` without ``BASE-TYPE-SIZE``
#: (skipped). Under ``--packages App`` only the walk can narrow the group. Invented
#: names only.
SELECTION_ONE_GROUP_WHOSE_TWIN_CANNOT_REGISTER_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Bits</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Uint16</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeA</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Ratio</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Bits/Uint16</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeB</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <SW-BASE-TYPE>
                  <SHORT-NAME>Halfword</SHORT-NAME>
                  <CATEGORY>FIXED_LENGTH</CATEGORY>
                  <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
                  <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
                </SW-BASE-TYPE>
                <IMPLEMENTATION-DATA-TYPE>
                  <SHORT-NAME>Ratio</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                  <SW-DATA-DEF-PROPS>
                    <SW-DATA-DEF-PROPS-VARIANTS>
                      <SW-DATA-DEF-PROPS-CONDITIONAL>
                        <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/TreeB/Types/Halfword</BASE-TYPE-REF>
                      </SW-DATA-DEF-PROPS-CONDITIONAL>
                    </SW-DATA-DEF-PROPS-VARIANTS>
                  </SW-DATA-DEF-PROPS>
                </IMPLEMENTATION-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>App</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Types</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-PRIMITIVE-DATA-TYPE>
                  <SHORT-NAME>Ratio</SHORT-NAME>
                  <CATEGORY>VALUE</CATEGORY>
                </APPLICATION-PRIMITIVE-DATA-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Maps</SHORT-NAME>
              <ELEMENTS>
                <DATA-TYPE-MAPPING-SET>
                  <SHORT-NAME>Set</SHORT-NAME>
                  <DATA-TYPE-MAPS>
                    <DATA-TYPE-MAP>
                      <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Types/Ratio</APPLICATION-DATA-TYPE-REF>
                      <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeA/Types/Ratio</IMPLEMENTATION-DATA-TYPE-REF>
                    </DATA-TYPE-MAP>
                    <DATA-TYPE-MAP>
                      <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Types/Ratio</APPLICATION-DATA-TYPE-REF>
                      <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/TreeB/Types/Ratio</IMPLEMENTATION-DATA-TYPE-REF>
                    </DATA-TYPE-MAP>
                  </DATA-TYPE-MAPS>
                </DATA-TYPE-MAPPING-SET>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Interfaces</SHORT-NAME>
              <ELEMENTS>
                <SENDER-RECEIVER-INTERFACE>
                  <SHORT-NAME>IRatio</SHORT-NAME>
                  <IS-SERVICE>false</IS-SERVICE>
                  <DATA-ELEMENTS>
                    <VARIABLE-DATA-PROTOTYPE>
                      <SHORT-NAME>value</SHORT-NAME>
                      <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">/Acme/App/Types/Ratio</TYPE-TREF>
                    </VARIABLE-DATA-PROTOTYPE>
                  </DATA-ELEMENTS>
                </SENDER-RECEIVER-INTERFACE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>Sensor</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pRatio</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/App/Interfaces/IRatio</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>SensorBehavior</SHORT-NAME>
                      <DATA-TYPE-MAPPING-REFS>
                        <DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">/Acme/App/Maps/Set</DATA-TYPE-MAPPING-REF>
                      </DATA-TYPE-MAPPING-REFS>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""

#: One component declared identically in two packages (a MERGED group) with a
#: composition placing the member the merge did not keep. ``Parts`` names the
#: non-primary declaration alone; ``TreeB`` names it with the composition above it.
#: Invented names only.
SELECTION_MERGED_COMPONENT_ARXML = """<?xml version="1.0" encoding="utf-8"?>
<AUTOSAR xmlns="http://autosar.org/schema/r4.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">
  <AR-PACKAGES>
    <AR-PACKAGE>
      <SHORT-NAME>Acme</SHORT-NAME>
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>Common</SHORT-NAME>
          <ELEMENTS>
            <SW-BASE-TYPE>
              <SHORT-NAME>Word</SHORT-NAME>
              <CATEGORY>FIXED_LENGTH</CATEGORY>
              <BASE-TYPE-SIZE>16</BASE-TYPE-SIZE>
              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>
              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>
            </SW-BASE-TYPE>
            <IMPLEMENTATION-DATA-TYPE>
              <SHORT-NAME>Level</SHORT-NAME>
              <CATEGORY>VALUE</CATEGORY>
              <SW-DATA-DEF-PROPS>
                <SW-DATA-DEF-PROPS-VARIANTS>
                  <SW-DATA-DEF-PROPS-CONDITIONAL>
                    <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/Common/Word</BASE-TYPE-REF>
                  </SW-DATA-DEF-PROPS-CONDITIONAL>
                </SW-DATA-DEF-PROPS-VARIANTS>
              </SW-DATA-DEF-PROPS>
            </IMPLEMENTATION-DATA-TYPE>
            <SENDER-RECEIVER-INTERFACE>
              <SHORT-NAME>ILevel</SHORT-NAME>
              <IS-SERVICE>false</IS-SERVICE>
              <DATA-ELEMENTS>
                <VARIABLE-DATA-PROTOTYPE>
                  <SHORT-NAME>level</SHORT-NAME>
                  <TYPE-TREF DEST="IMPLEMENTATION-DATA-TYPE">/Acme/Common/Level</TYPE-TREF>
                </VARIABLE-DATA-PROTOTYPE>
              </DATA-ELEMENTS>
            </SENDER-RECEIVER-INTERFACE>
          </ELEMENTS>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeA</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Components</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>Sensor</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pLevel</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Common/ILevel</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>Sensor_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
        <AR-PACKAGE>
          <SHORT-NAME>TreeB</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>Parts</SHORT-NAME>
              <ELEMENTS>
                <APPLICATION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>Sensor</SHORT-NAME>
                  <PORTS>
                    <P-PORT-PROTOTYPE>
                      <SHORT-NAME>pLevel</SHORT-NAME>
                      <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">/Acme/Common/ILevel</PROVIDED-INTERFACE-TREF>
                    </P-PORT-PROTOTYPE>
                  </PORTS>
                  <INTERNAL-BEHAVIORS>
                    <SWC-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>Sensor_InternalBehavior</SHORT-NAME>
                      <HANDLE-TERMINATION-AND-RESTART>NO-SUPPORT</HANDLE-TERMINATION-AND-RESTART>
                      <SUPPORTS-MULTIPLE-INSTANTIATION>false</SUPPORTS-MULTIPLE-INSTANTIATION>
                    </SWC-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </APPLICATION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Comps</SHORT-NAME>
              <ELEMENTS>
                <COMPOSITION-SW-COMPONENT-TYPE>
                  <SHORT-NAME>Wrap</SHORT-NAME>
                  <COMPONENTS>
                    <SW-COMPONENT-PROTOTYPE>
                      <SHORT-NAME>sensor</SHORT-NAME>
                      <TYPE-TREF DEST="APPLICATION-SW-COMPONENT-TYPE">/Acme/TreeB/Parts/Sensor</TYPE-TREF>
                    </SW-COMPONENT-PROTOTYPE>
                  </COMPONENTS>
                </COMPOSITION-SW-COMPONENT-TYPE>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AR-PACKAGE>
  </AR-PACKAGES>
</AUTOSAR>
"""


class TestAPackagesSelectionAfterTheVerdict:
    """The verdict is taken over the whole file, the selection narrows it.

    A conflicting group of which the selection keeps exactly **one** member is
    re-admitted as that member and its conflict line suppressed; keeping **two**
    keeps the conflict. *Keeps* is read by package first, and only when the
    selection names no member is the reference graph asked which member is reached.
    A reference to the excluded member binds to nothing (the two differ) and is kept
    as unresolved, *outside the selected packages*, a cause produced here alone.
    """

    SHAPES_DIR = Path(__file__).parent / "fixtures" / "arxml_import_shapes"
    #: Two parallel trees, App typed out of TreeA alone.
    CONFLICT = SHAPES_DIR / "packages_selection_conflict.arxml"

    @staticmethod
    def _sections(rendered: str) -> dict[str, list[str]]:
        headings = ("Merged declarations", "Conflicting declarations", "Unresolved references")
        lines = rendered.split("\n")
        sections: dict[str, list[str]] = {}
        for index, line in enumerate(lines):
            for heading in headings:
                if line.startswith(heading):
                    end = lines.index("", index) if "" in lines[index:] else len(lines)
                    sections[heading] = lines[index:end]
        return sections

    def test_a_selection_keeping_one_member_of_a_conflicting_group_imports_that_member(self, tmp_path: Path) -> None:
        """`--packages App` names neither tree, so the reference graph decides which member survives."""
        project = _init_project(tmp_path)
        model_root = _model_root(project)

        report = run_import(str(self.CONFLICT), str(project / "swcraft.yaml"), packages=["App"])

        assert report.conflicting_groups == []
        assert report.unresolved_references == []
        assert "Conflicting declarations" not in self._sections(report.render())
        # Suppressed is not silent: the user asked for neither declaration by name.
        assert [line for line in report.closure_lines if "re-admitted" in line] == [
            "2 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "base_types/SW-BASE-TYPE/Word at /Acme/TreeA/Types/Word, "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Ratio at /Acme/TreeA/Types/Ratio"
        ]
        assert (model_element_path(model_root, "Word", kind="base_type")).is_file()
        assert (model_element_path(model_root, "Ratio", kind="value_type")).is_file()
        assert (
            yaml.safe_load((model_element_path(model_root, "Word", kind="base_type")).read_text(encoding="utf-8"))[
                "size"
            ]
            == 16
        )
        assert (
            yaml.safe_load((model_element_path(model_root, "Ratio", kind="value_type")).read_text(encoding="utf-8"))[
                "base"
            ]
            == "Word"
        )
        assert (report.imported_data_types, report.imported_interfaces, report.imported_swcs) == (2, 1, 1)

        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_a_re_admission_the_closure_prunes_is_not_announced_as_one(self, tmp_path: Path) -> None:
        """A re-admission the closure prunes is not announced as one, and the prune has its own reason.

        Not *outside the selection*: that is false of a package the user named.
        """
        project = _init_project(tmp_path)

        report = run_import(str(self.CONFLICT), str(project / "swcraft.yaml"), packages=["App", "TreeB"])

        assert report.conflicting_groups == []
        # Suppressing a conflict line and then saying nothing would leave the clean
        # report the section exists to prevent.
        assert [line for line in report.closure_lines if "re-admitted" in line] == [
            "2 conflicting name group(s) re-admitted and then excluded (the selection keeps one "
            "declaration of each, and nothing it kept references that declaration): "
            "base_types/SW-BASE-TYPE/Word at /Acme/TreeB/Types/Word, "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Ratio at /Acme/TreeB/Types/Ratio"
        ]
        assert [line for line in report.closure_lines if "data type(s) excluded" in line] == [
            "2 data type(s) excluded (inside the selection, but nothing the selection kept "
            "references them): Ratio, Word"
        ]
        # No data type is written, which is why the line above is the truthful one.
        assert (report.imported_data_types, report.imported_interfaces, report.imported_swcs) == (0, 1, 1)
        assert [(record.target, record.cause) for record in report.unresolved_references] == [
            ("/Acme/TreeA/Types/Ratio", CAUSE_SELECTION)
        ]
        assert not model_element_path(_model_root(project), "Ratio", layer="implementation_types").exists()

    def test_a_declaration_outside_the_selection_keeps_the_older_sentence(self, tmp_path: Path) -> None:
        """A declaration outside the selection keeps the older sentence, so neither reason swallows the other."""
        project = _init_project(tmp_path)

        report = run_import(str(self.CONFLICT), str(project / "swcraft.yaml"), packages=["App", "TreeA"])

        assert [line for line in report.closure_lines if "re-admitted" in line] == [
            "2 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "base_types/SW-BASE-TYPE/Word at /Acme/TreeA/Types/Word, "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Ratio at /Acme/TreeA/Types/Ratio"
        ]
        assert [line for line in report.closure_lines if "and then excluded" in line] == []
        assert [line for line in report.closure_lines if "nothing the selection kept" in line] == []
        assert (report.imported_data_types, report.imported_interfaces, report.imported_swcs) == (2, 1, 1)

    def test_a_selection_keeping_both_members_keeps_the_conflict(self, tmp_path: Path) -> None:
        """`--packages Acme` is both trees, so the verdict stands and the reference reads as the conflict."""
        project = _init_project(tmp_path)

        report = run_import(str(self.CONFLICT), str(project / "swcraft.yaml"), packages=["Acme"])

        assert [str(group.key) for group in report.conflicting_groups] == [
            "base_types/SW-BASE-TYPE/Word",
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Ratio",
        ]
        assert [record.cause for record in report.unresolved_references] == [CAUSE_CONFLICT]
        assert not model_element_path(_model_root(project), "Ratio", layer="implementation_types").exists()

    def test_a_selection_excluding_a_target_names_it_outside_the_selected_packages(self, tmp_path: Path) -> None:
        """A reference to the declaration the selection excluded is kept, outside the selected packages."""
        source = tmp_path / "excluded_target.arxml"
        source.write_text(SELECTION_EXCLUDED_TARGET_ARXML, encoding="utf-8")
        project = _init_project(tmp_path)

        report = run_import(str(source), str(project / "swcraft.yaml"), packages=["App", "TreeA"])

        assert report.conflicting_groups == []
        assert [(record.target, record.cause) for record in report.unresolved_references] == [
            ("/Acme/TreeB/Types/Ratio", CAUSE_SELECTION)
        ]
        section = self._sections(report.render())["Unresolved references"]
        assert section[1] == (
            "  - /Acme/TreeB/Types/Ratio (DEST IMPLEMENTATION-DATA-TYPE) is outside the selected "
            "packages; 1 referring element(s):"
        )
        elements = yaml.safe_load((_model_root(project) / "interfaces" / "IRatio.yaml").read_text(encoding="utf-8"))[
            "data_elements"
        ]
        assert [element["implementation_type"] for element in elements] == ["Ratio", "/Acme/TreeB/Types/Ratio"]

    def test_a_narrowed_import_reports_only_the_references_its_own_project_carries(self, tmp_path: Path) -> None:
        """An unresolved record or external stub every referrer of which the selection dropped is gone."""
        source = tmp_path / "narrowed.arxml"
        source.write_text(SELECTION_NARROWED_REPORT_ARXML, encoding="utf-8")

        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=["GroupA"])

        assert whole.unresolved_references == []
        assert [(stub.target, len(stub.referrers)) for stub in whole.external_type_stubs] == [
            ("/Acme/Elsewhere/LevelB", 1)
        ]
        assert narrowed.external_type_stubs == []
        assert not model_element_path(_model_root(narrowed_project), "LevelB", layer="implementation_types").exists()
        assert narrowed.unresolved_references == []
        assert "Unresolved references" not in self._sections(narrowed.render())
        assert "UNRESOLVED " not in narrowed.render()
        result = check_cross_layer_consistency(model_root=str(_model_root(narrowed_project)))
        assert result.consistent, result.issues

    def test_a_selection_narrows_a_conflicting_connector_group_too(self, tmp_path: Path) -> None:
        """A conflicting connector group is narrowed like every other kind that takes a verdict.

        Without connectors in ``selection_reach`` the selected connector would be
        dropped and the conflict line would disclose a filtered-out declaration.
        """
        source = tmp_path / "connector_conflict.arxml"
        source.write_text(SELECTION_CONNECTOR_CONFLICT_ARXML, encoding="utf-8")

        # Over the whole file the verdict stands: both declarations are dropped.
        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        assert [str(group.key) for group in whole.conflicting_groups] == ["connectors/ASSEMBLY-SW-CONNECTOR/Wire"]
        assert whole.imported_connectors == 0

        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=["TreeA"])

        assert narrowed.conflicting_groups == []
        assert "Conflicting declarations" not in self._sections(narrowed.render())
        assert [line for line in narrowed.closure_lines if "re-admitted" in line] == [
            "1 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "connectors/ASSEMBLY-SW-CONNECTOR/Wire at /Acme/TreeA/Cell/Wire"
        ]
        assert narrowed.imported_connectors == 1
        # TreeA's member: the endpoints name the prototypes of `Cell`, never `Cell2`'s.
        written = (_model_root(narrowed_project) / "connectors" / "Wire.yaml").read_text(encoding="utf-8")
        connector = yaml.safe_load(written)
        assert connector["composition"] == "Cell"
        result = check_cross_layer_consistency(model_root=str(_model_root(narrowed_project)))
        assert result.consistent, result.issues

    def test_a_selection_reaches_a_group_through_an_application_primitives_mapping_set(self, tmp_path: Path) -> None:
        """The closure walk follows an application primitive's ``DATA-TYPE-MAP`` entry.

        Without that edge the implementation type the mapping set pairs it with is
        dropped and ``rule:data-type.application-type-unmapped`` reports.
        """
        source = tmp_path / "mapping_set_reach.arxml"
        source.write_text(SELECTION_MAPPING_SET_REACH_ARXML, encoding="utf-8")

        # Over the whole file both groups conflict; the primitive and its interface
        # are still written, with the map spelled as the export spelled it.
        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        assert [str(group.key) for group in whole.conflicting_groups] == [
            "base_types/SW-BASE-TYPE/Word",
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Raw",
        ]
        assert (whole.imported_data_types, whole.imported_interfaces) == (1, 1)
        assert [(record.target, record.cause) for record in whole.unresolved_references] == [
            ("/Acme/Left/Raw", CAUSE_CONFLICT)
        ]

        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=["App"])

        # The entry names Left's declaration, so Left's is the member each group
        # keeps, and the walk carries on to the base type under it.
        assert narrowed.conflicting_groups == []
        assert [line for line in narrowed.closure_lines if "re-admitted" in line] == [
            "2 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "base_types/SW-BASE-TYPE/Word at /Acme/Left/Word, "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Raw at /Acme/Left/Raw"
        ]
        assert (narrowed.imported_data_types, narrowed.imported_interfaces) == (3, 1)
        model_root = _model_root(narrowed_project)
        sensor = yaml.safe_load((model_root / "swc" / "App" / "Sensor.yaml").read_text(encoding="utf-8"))
        assert [port["interface"] for port in sensor["ports"]] == ["ISpeed"]
        # The chain the entry names is in the project: the physical view, its
        # implementation type and that type's base type.
        speed = yaml.safe_load(
            (model_element_path(model_root, "Speed", kind="application_type")).read_text(encoding="utf-8")
        )
        raw = yaml.safe_load((model_element_path(model_root, "Raw", kind="value_type")).read_text(encoding="utf-8"))
        assert speed["kind"] == "application_type"
        assert (raw["kind"], raw["base"]) == ("value_type", "Word")

    def test_a_selection_reaches_the_target_the_mapping_layer_falls_back_to(self, tmp_path: Path) -> None:
        """The walk reaches the generic entry the mapping layer falls back to when the twin is excluded.

        The twin branch requires ``registry.resolve(twin) is not None``, decided after
        ``narrow_to_selection``, so an excluded twin falls through to the generic entry.
        Without this edge the **singleton** primitive (which takes no verdict) would be
        skipped with its interface and port.
        """
        source = tmp_path / "mapping_set_fall_back.arxml"
        source.write_text(SELECTION_MAPPING_SET_FALL_BACK_ARXML, encoding="utf-8")

        # Over the whole file both groups conflict.
        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        assert [str(group.key) for group in whole.conflicting_groups] == [
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Raw",
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed",
        ]

        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=["App", "TreeA"])

        assert narrowed.conflicting_groups == []
        # Both groups are re-admitted. The excluded twin's own group is pruned:
        # the primitive falls through to the GENERIC entry, so nothing kept
        # references that declaration.
        assert [line for line in narrowed.closure_lines if "re-admitted" in line] == [
            "1 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Raw at /Acme/GenLeft/Raw",
            "1 conflicting name group(s) re-admitted and then excluded (the selection keeps one "
            "declaration of each, and nothing it kept references that declaration): "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed at /Acme/TreeA/Speed",
        ]
        assert (narrowed.imported_data_types, narrowed.imported_interfaces) == (3, 1)
        model_root = _model_root(narrowed_project)
        sensor = yaml.safe_load((model_root / "swc" / "App" / "Sensor.yaml").read_text(encoding="utf-8"))
        assert [port["interface"] for port in sensor["ports"]] == ["ISpeed"]
        # The re-admitted target is in the project over its own base type; the
        # primitive is written under its declared name (another name space than
        # the excluded twin's `Speed`).
        primitive = yaml.safe_load(
            (model_element_path(model_root, "Speed", kind="application_type")).read_text(encoding="utf-8")
        )
        assert not model_element_path(model_root, "Speed", kind="value_type").exists()
        raw = yaml.safe_load((model_element_path(model_root, "Raw", kind="value_type")).read_text(encoding="utf-8"))
        assert primitive["kind"] == "application_type"
        assert (raw["kind"], raw["base"]) == ("value_type", "Octet")

    def test_a_fall_back_inside_the_twins_own_group_is_not_walked_a_second_time(self, tmp_path: Path) -> None:
        """A fall-back inside the twin's own group is not walked a second time.

        Reaching both members would leave the group conflicting and drop the
        primitive, its interface and port; whichever member the group is narrowed to,
        the layer resolves it. Fails if the walk follows both candidates unconditionally.
        """
        source = tmp_path / "mapping_set_one_group.arxml"
        source.write_text(SELECTION_MAPPING_SET_FALL_BACK_IN_ONE_GROUP_ARXML, encoding="utf-8")

        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        assert [str(group.key) for group in whole.conflicting_groups] == [
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed"
        ]

        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=["App"])

        # The twin entry is the last one, so it names TreeB's declaration.
        assert narrowed.conflicting_groups == []
        assert [line for line in narrowed.closure_lines if "re-admitted" in line] == [
            "1 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed at /Acme/TreeB/Speed"
        ]
        assert (narrowed.imported_data_types, narrowed.imported_interfaces, narrowed.imported_swcs) == (2, 1, 1)
        model_root = _model_root(narrowed_project)
        # The primitive collapsed onto its twin: one element, no `_app` name.
        speed = yaml.safe_load((model_element_path(model_root, "Speed", kind="value_type")).read_text(encoding="utf-8"))
        assert (speed["kind"], speed["base"]) == ("value_type", "WordB")
        assert not model_element_path(model_root, "Speed_app", layer="implementation_types").exists()
        interface = yaml.safe_load((model_root / "interfaces" / "ISpeed.yaml").read_text(encoding="utf-8"))
        assert [element["implementation_type"] for element in interface["data_elements"]] == ["Speed"]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    @pytest.mark.parametrize(
        ("packages", "member", "base_size"),
        [
            pytest.param(["App"], "/Acme/TreeB/Speed", 32, id="reached-chooses-the-twin"),
            pytest.param(["App", "TreeA"], "/Acme/TreeA/Speed", 8, id="the-packages-choose-the-fall-back"),
        ],
    )
    def test_the_one_group_exception_follows_the_selections_own_precedence(
        self, tmp_path: Path, packages: list[str], member: str, base_size: int
    ) -> None:
        """The walk's one exception follows ``narrow_to_selection``'s precedence: *selected* first, then *reached*.

        ``--packages App`` names neither member, so the walk narrows to the twin;
        ``--packages App,TreeA`` names one, and that member's base must be walked, or
        its base is left an unresolved ``outside-selection`` reference ``check-model``
        refuses. The second case fails for a walk that reads *reached* alone.
        """
        source = tmp_path / "mapping_set_one_group_one_base_group.arxml"
        source.write_text(SELECTION_MAPPING_SET_ONE_GROUP_OVER_ONE_BASE_GROUP_ARXML, encoding="utf-8")

        # Over the whole file both groups conflict: the base types by size, the
        # implementation types by the verdict's edge inheritance.
        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        assert [str(group.key) for group in whole.conflicting_groups] == [
            "base_types/SW-BASE-TYPE/Word",
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed",
        ]

        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=packages)

        assert narrowed.conflicting_groups == []
        assert narrowed.unresolved_references == []
        base = "/Acme/BaseA/Word" if base_size == 8 else "/Acme/BaseB/Word"
        assert [line for line in narrowed.closure_lines if "re-admitted" in line] == [
            "2 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            f"base_types/SW-BASE-TYPE/Word at {base}, implementation_types/IMPLEMENTATION-DATA-TYPE/Speed at {member}"
        ]
        model_root = _model_root(narrowed_project)
        # The value type rests on the kept base type by NAME, not on an
        # undeclared ARXML path.
        speed = yaml.safe_load((model_element_path(model_root, "Speed", kind="value_type")).read_text(encoding="utf-8"))
        word = yaml.safe_load((model_element_path(model_root, "Word", kind="base_type")).read_text(encoding="utf-8"))
        assert (speed["kind"], speed["base"]) == ("value_type", "Word")
        assert (word["kind"], word["size"]) == ("base_type", base_size)
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_a_merged_one_group_pair_the_packages_name_both_of_still_re_admits_below_it(self, tmp_path: Path) -> None:
        """A **merged** one-group pair both members of which are selected still re-admits what it rests on.

        The verdict's edge inheritance does not reach past the **singleton** below it, so without
        the edge the conflicting group further down stays conflicting, ``check-model``
        refuses the project and the conflict line names an excluded package.
        """
        source = tmp_path / "merged_one_group_over_a_singleton.arxml"
        source.write_text(SELECTION_MERGED_ONE_GROUP_OVER_A_SINGLETON_ARXML, encoding="utf-8")

        # Over the whole file the Speed group conflicts: the merged Word group
        # keeps its verdict across the singleton between them.
        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        assert [str(group.key) for group in whole.merged_groups] == [
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Word"
        ]
        assert [str(group.key) for group in whole.conflicting_groups] == [
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed"
        ]

        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=["Selected"])

        assert narrowed.conflicting_groups == []
        assert narrowed.unresolved_references == []
        assert [line for line in narrowed.closure_lines if "re-admitted" in line] == [
            "1 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Speed at /Acme/TreeX/Types/Speed"
        ]
        model_root = _model_root(narrowed_project)
        # The primary's chain resolves by NAME the whole way down.
        level = yaml.safe_load(
            (model_element_path(model_root, "Level", kind="reference_type")).read_text(encoding="utf-8")
        )
        speed = yaml.safe_load((model_element_path(model_root, "Speed", kind="value_type")).read_text(encoding="utf-8"))
        assert (level["kind"], level["base"]) == ("reference_type", "Speed")
        assert (speed["kind"], speed["base"]) == ("value_type", "Uint16")
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    @pytest.mark.parametrize(
        "packages",
        [
            pytest.param(["App"], id="reached-chooses-the-member"),
            pytest.param(["App", "TreeA"], id="the-packages-choose-the-member"),
        ],
    )
    def test_a_one_group_row_is_narrowed_to_the_member_that_can_register(
        self, tmp_path: Path, packages: list[str]
    ) -> None:
        """Where the walk alone narrows a conflicting one-group row, it declares the member that can register.

        Declaring the twin that registers nothing would exclude the generic entry and
        skip the primitive, its interface and port while the report announced a
        re-admission (and ``check-model`` stays green). ``App TreeA`` takes the choice
        away from the walk; one assertion holds both parameters.
        """
        source = tmp_path / "one_group_whose_twin_cannot_register.arxml"
        source.write_text(SELECTION_ONE_GROUP_WHOSE_TWIN_CANNOT_REGISTER_ARXML, encoding="utf-8")

        # Over the whole file the group conflicts and nothing narrows it.
        whole = run_import(str(source), str(_init_project(tmp_path, "whole") / "swcraft.yaml"))
        assert [str(group.key) for group in whole.conflicting_groups] == [
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Ratio"
        ]

        narrowed_project = _init_project(tmp_path, "narrowed")
        narrowed = run_import(str(source), str(narrowed_project / "swcraft.yaml"), packages=packages)

        assert narrowed.conflicting_groups == []
        assert narrowed.unresolved_references == []
        # The member re-admitted is the one that produces an element.
        assert [line for line in narrowed.closure_lines if "re-admitted" in line] == [
            "1 conflicting name group(s) re-admitted (the selection keeps one declaration of each): "
            "implementation_types/IMPLEMENTATION-DATA-TYPE/Ratio at /Acme/TreeA/Types/Ratio"
        ]
        assert (narrowed.imported_data_types, narrowed.imported_interfaces, narrowed.imported_swcs) == (3, 1, 1)
        model_root = _model_root(narrowed_project)
        ratio = yaml.safe_load((model_element_path(model_root, "Ratio", kind="value_type")).read_text(encoding="utf-8"))
        assert (ratio["kind"], ratio["base"]) == ("value_type", "Uint16")
        sensor = yaml.safe_load((model_root / "swc" / "Components" / "Sensor.yaml").read_text(encoding="utf-8"))
        assert [(port["name"], port["interface"]) for port in sensor["ports"]] == [("pRatio", "IRatio")]
        result = check_cross_layer_consistency(model_root=str(model_root))
        assert result.consistent, result.issues

    def test_a_selection_naming_a_merged_groups_other_declaration_keeps_the_element(self, tmp_path: Path) -> None:
        """A merged group is one element every member path resolves to, for the selection as well.

        A lookup by the selected declaration's own path alone would refuse a selection
        of the non-primary package and prune the component out of a wider one.
        """
        source = tmp_path / "merged_component.arxml"
        source.write_text(SELECTION_MERGED_COMPONENT_ARXML, encoding="utf-8")

        # The package holding the non-primary declaration ALONE: one component,
        # its interface and the two types under it, and no refusal.
        parts_project = _init_project(tmp_path, "parts")
        parts = run_import(str(source), str(parts_project / "swcraft.yaml"), packages=["Parts"])

        assert [str(group.key) for group in parts.merged_groups] == ["swc/APPLICATION-SW-COMPONENT-TYPE/Sensor"]
        assert (parts.imported_swcs, parts.imported_interfaces, parts.imported_data_types) == (1, 1, 2)
        # The merged element is written in the PRIMARY declaration's package
        # directory: one element every member path resolves to.
        assert (_model_root(parts_project) / "swc" / "Components" / "Sensor.yaml").exists()

        # ...and the package holding it together with the composition that places
        # it keeps the member.
        wrap_project = _init_project(tmp_path, "wrap")
        wrap = run_import(str(source), str(wrap_project / "swcraft.yaml"), packages=["TreeB"])

        assert wrap.imported_swcs == 1
        composition = yaml.safe_load(
            (_model_root(wrap_project) / "compositions" / "Wrap.yaml").read_text(encoding="utf-8")
        )
        assert [(member["name"], member["type"]) for member in composition["members"]] == [("sensor", "Sensor")]
        result = check_cross_layer_consistency(model_root=str(_model_root(wrap_project)))
        assert result.consistent, result.issues


class TestNoAlternativeAnswerForTheWalkKeepsStrictlyMore:
    """A **bounded differential** over ``merge._walked_chain``'s one answer.

    Each corpus shape is imported under each selection once per candidate answer
    (twin, fallback, both, neither, the group's primary, the product's), and no
    alternative may keep strictly more: both its sets -- in-selection declarations
    skipped, and ``(target, cause)`` of the unresolved records -- are subsets of the
    product's, one properly. *Kept more* is not *wrote more*: a twin collapse writes
    one element where a generic step writes two.

    The corpus: generic entry on the twin's group or another; twin's and fallback's
    base type with or without ``BASE-TYPE-SIZE``; four selections. Each candidate
    rests on its own declaration of one base-type ``SHORT-NAME``.

    Does not reach:

    * the **merged** one-group row over a singleton (no choice is observable without
      a third level; the merged one-group test above pins it);
    * every ``selection_reach`` edge but the ``DATA-TYPE-MAP`` one;
    * compositions, connectors and requirements;
    * a loss every candidate suffers alike: the oracle is domination, not correctness.
    """

    #: The rows where an alternative keeps strictly more, a decision rather than a
    #: defect: the two-group row's over-reach. The walk declares **both**
    #: candidates (telling which branch the mapping layer takes needs the twin's
    #: narrowing, the cycle ``reachable_mapping_targets`` refuses), so a third group
    #: both land in stays conflicting.
    #:
    #: Asserted as a SET both ways: a new dominated row fails, and so does one of
    #: these ceasing to be dominated.
    KNOWN_DOMINATED = {
        "two-groups/twin-registers/fallback-registers/App",
        "two-groups/twin-registers/fallback-refused/App",
    }

    ANSWERS = ("actual", "twin", "fallback", "both", "neither", "primary")

    @staticmethod
    def _shape(*, same_name: bool, twin_registers: bool, fallback_registers: bool) -> str:
        """One corpus row as ARXML. Invented names only.

        The mapping set names the fallback first (the generic entry ``setdefault``
        keeps) and the twin last (which plain assignment keeps).
        """

        def base(size: int | None) -> str:
            size_line = f"              <BASE-TYPE-SIZE>{size}</BASE-TYPE-SIZE>\n" if size else ""
            return (
                "            <SW-BASE-TYPE>\n"
                "              <SHORT-NAME>Word</SHORT-NAME>\n"
                "              <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
                f"{size_line}"
                "              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
                "              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>\n"
                "            </SW-BASE-TYPE>\n"
            )

        def impl(name: str, tree: str) -> str:
            return (
                "            <IMPLEMENTATION-DATA-TYPE>\n"
                f"              <SHORT-NAME>{name}</SHORT-NAME>\n"
                "              <CATEGORY>VALUE</CATEGORY>\n"
                "              <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS>\n"
                "              <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                f'                <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/{tree}/Types/Word</BASE-TYPE-REF>\n'
                "              </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                "              </SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>\n"
                "            </IMPLEMENTATION-DATA-TYPE>\n"
            )

        fallback_name = "Ratio" if same_name else "Other"
        twin_tree = (
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>TreeT</SHORT-NAME>\n"
            "          <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME>\n"
            "          <ELEMENTS>\n"
            f"{base(16 if twin_registers else None)}{impl('Ratio', 'TreeT')}"
            "          </ELEMENTS>\n"
            "          </AR-PACKAGE></AR-PACKAGES>\n"
            "        </AR-PACKAGE>\n"
        )
        fallback_tree = (
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>TreeF</SHORT-NAME>\n"
            "          <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME>\n"
            "          <ELEMENTS>\n"
            f"{base(32 if fallback_registers else None)}{impl(fallback_name, 'TreeF')}"
            "          </ELEMENTS>\n"
            "          </AR-PACKAGE></AR-PACKAGES>\n"
            "        </AR-PACKAGE>\n"
        )
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">\n'
            "  <AR-PACKAGES>\n"
            "    <AR-PACKAGE>\n"
            "      <SHORT-NAME>Acme</SHORT-NAME>\n"
            "      <AR-PACKAGES>\n"
            f"{twin_tree}{fallback_tree}"
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>App</SHORT-NAME>\n"
            "          <AR-PACKAGES>\n"
            "            <AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME><ELEMENTS>\n"
            "              <APPLICATION-PRIMITIVE-DATA-TYPE>\n"
            "                <SHORT-NAME>Ratio</SHORT-NAME><CATEGORY>VALUE</CATEGORY>\n"
            "              </APPLICATION-PRIMITIVE-DATA-TYPE>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
            "            <AR-PACKAGE><SHORT-NAME>Maps</SHORT-NAME><ELEMENTS>\n"
            "              <DATA-TYPE-MAPPING-SET><SHORT-NAME>Set</SHORT-NAME><DATA-TYPE-MAPS>\n"
            "                <DATA-TYPE-MAP>\n"
            '                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            "/Acme/App/Types/Ratio</APPLICATION-DATA-TYPE-REF>\n"
            '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            f"/Acme/TreeF/Types/{fallback_name}</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "                </DATA-TYPE-MAP>\n"
            "                <DATA-TYPE-MAP>\n"
            '                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            "/Acme/App/Types/Ratio</APPLICATION-DATA-TYPE-REF>\n"
            '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
            "/Acme/TreeT/Types/Ratio</IMPLEMENTATION-DATA-TYPE-REF>\n"
            "                </DATA-TYPE-MAP>\n"
            "              </DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
            "            <AR-PACKAGE><SHORT-NAME>Interfaces</SHORT-NAME><ELEMENTS>\n"
            "              <SENDER-RECEIVER-INTERFACE><SHORT-NAME>IRatio</SHORT-NAME>\n"
            "                <IS-SERVICE>false</IS-SERVICE>\n"
            "                <DATA-ELEMENTS><VARIABLE-DATA-PROTOTYPE><SHORT-NAME>value</SHORT-NAME>\n"
            '                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            "/Acme/App/Types/Ratio</TYPE-TREF>\n"
            "                </VARIABLE-DATA-PROTOTYPE></DATA-ELEMENTS>\n"
            "              </SENDER-RECEIVER-INTERFACE>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
            "            <AR-PACKAGE><SHORT-NAME>Components</SHORT-NAME><ELEMENTS>\n"
            "              <APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>Sensor</SHORT-NAME><PORTS>\n"
            "                <P-PORT-PROTOTYPE><SHORT-NAME>pRatio</SHORT-NAME>\n"
            '                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">'
            "/Acme/App/Interfaces/IRatio</PROVIDED-INTERFACE-TREF>\n"
            "                </P-PORT-PROTOTYPE>\n"
            "              </PORTS></APPLICATION-SW-COMPONENT-TYPE>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
            "          </AR-PACKAGES>\n"
            "        </AR-PACKAGE>\n"
            "      </AR-PACKAGES>\n"
            "    </AR-PACKAGE>\n"
            "  </AR-PACKAGES>\n"
            "</AUTOSAR>\n"
        )

    @staticmethod
    def _answer(kind: str, real):
        """One candidate answer, in ``merge._walked_chain``'s own signature."""

        def walked(groups, decisions, package_selected, twin, fallback, *, certain, certain_set):
            if kind == "actual":
                return real(
                    groups, decisions, package_selected, twin, fallback, certain=certain, certain_set=certain_set
                )
            if kind == "twin":
                return (twin,)
            if kind == "fallback":
                return (fallback,)
            if kind == "both":
                return (twin, fallback)
            if kind == "neither":
                return ()
            group = decisions.group_of(twin) if twin is not None else None
            return (group.primary,) if group is not None else (twin,)

        return walked

    def test_no_alternative_answer_keeps_strictly_more_than_the_one_the_walk_gives(
        self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No alternative dominates the walk's answer, but for the two rows it is allowed to lose."""
        from swcraft_tools.arxml_import import merge as merge_module
        from swcraft_tools.arxml_import.report import in_selected_package

        real = merge_module._walked_chain
        root = tmp_path_factory.mktemp("walk-differential")
        manifest = str(_init_project(root, "project") / "swcraft.yaml")
        source = root / "shape.arxml"
        selections = (("App",), ("App", "TreeT"), ("App", "TreeF"), ("App", "TreeT", "TreeF"))

        dominated: dict[str, list[str]] = {}
        answers_that_differ = 0
        for same_name, twin_registers, fallback_registers in itertools.product((True, False), repeat=3):
            source.write_text(
                self._shape(
                    same_name=same_name,
                    twin_registers=twin_registers,
                    fallback_registers=fallback_registers,
                ),
                encoding="utf-8",
            )
            for packages in selections:
                losses: dict[str, tuple[frozenset[str], frozenset[tuple[str, str]]]] = {}
                for kind in self.ANSWERS:
                    monkeypatch.setattr(merge_module, "_walked_chain", self._answer(kind, real))
                    report = run_import(str(source), manifest, packages=list(packages), dry_run=True)
                    # A descriptive (``report``) entry documents an element the import
                    # keeps or collapses, so it is no skipped declaration.
                    losses[kind] = (
                        frozenset(
                            entry.path
                            for entry in report.skip_entries
                            if entry.effect == "abort" and in_selected_package(entry.path, set(packages))
                        ),
                        frozenset((record.target, record.cause) for record in report.unresolved_references),
                    )
                monkeypatch.setattr(merge_module, "_walked_chain", real)

                actual = losses["actual"]
                row = "/".join(
                    (
                        "one-group" if same_name else "two-groups",
                        "twin-registers" if twin_registers else "twin-refused",
                        "fallback-registers" if fallback_registers else "fallback-refused",
                        "+".join(packages),
                    )
                )
                better = [
                    kind
                    for kind in self.ANSWERS[1:]
                    if losses[kind][0] <= actual[0]
                    and losses[kind][1] <= actual[1]
                    and (losses[kind][0] < actual[0] or losses[kind][1] < actual[1])
                ]
                if better:
                    dominated[row] = better
                answers_that_differ += len({losses[kind] for kind in self.ANSWERS[1:]}) - 1

        # The seam is live: a green run is a measurement, not a monkeypatch that
        # missed its target.
        assert answers_that_differ > 0
        assert set(dominated) == self.KNOWN_DOMINATED, dominated


class TestNoAlternativeAnswerForTheMappingSetWalkKeepsStrictlyMore:
    """The **second** bounded differential, over the mapping set's answers in the ``--packages`` walk.

    It measures the mapping set element, for which ``report._mapping_set_targets``
    declares no outgoing edge. Method and domination relation are the first
    differential's; the candidates are ``(pair answer, set answer)``: the pair's
    twin, nothing, or the product's; the set's nothing, both sides of every map,
    or either side alone.

    The corpus: the same-name pair collapsed (bare ``Ratio``) or kept (with a
    ``UNIT-REF``); its two declarations merged or conflicting; the set's second map
    inside or outside the selected package; four selections. Each row is a
    ``--dry-run``.

    Does not vary:

    * the number of sets (one per row; ``test_arxml_import_mapping_sets.py`` holds more);
    * the kinds a map may name (no ``STRUCTURE``/``ARRAY`` target, no application ``enum``);
    * the verdict on the set itself (``differing_mapping_sets.arxml``'s shape);
    * compositions, connectors, requirements and interfaces beyond the one;
    * the order of a set's maps;
    * a loss every candidate suffers alike: the oracle is domination, not correctness.
    """

    #: The rows where an alternative keeps strictly more, a decision rather than a
    #: defect: the first differential's over-reach one level out. Two edges' targets
    #: rest on one base-type ``SHORT-NAME``, so that group stays conflicting.
    #: Declaring the pair's twin alone keeps one record fewer here but loses one on
    #: the ``physics/*/inside/App`` rows, so neither dominates and the answer that
    #: re-admits more stands (the fail-closed direction).
    #:
    #: Asserted as a SET both ways: a new dominated row fails, and so does one of
    #: these ceasing to be dominated.
    KNOWN_DOMINATED = {
        "bare/identical/outside/App",
        "bare/differing/outside/App",
    }

    PAIR_ANSWERS = ("actual", "twin", "neither")
    SET_ANSWERS = ("actual", "both-sides", "application", "implementation")

    #: On how many of the 32 rows each seam is **live** (some alternative produces a
    #: different loss set), measured on this tree and moved by the corpus; `>=`
    #: because live on more rows is no regression, and needed because the aggregate
    #: assertions are satisfied by a seam live on ONE row. Does not establish: that
    #: the set seam is exercised across the axes -- it is live on exactly one row
    #: (only where a map's two sides are narrowed apart).
    MINIMUM_LIVE_PAIR_ROWS = 6
    MINIMUM_LIVE_SET_ROWS = 1

    @staticmethod
    def _shape(*, bare: bool, identical: bool, inside: bool) -> str:
        """One corpus row as ARXML. Invented names only.

        ``/Acme/Alt`` holds ``Ratio``'s second declaration, ``/Acme/TreeT`` the
        implementation twin and the unit, ``/Acme/TreeF`` the outside target of the
        second map; each tree declares its own ``Word``, so narrowing is visible.
        """

        def base(package: str, size: int) -> str:
            return (
                "            <SW-BASE-TYPE>\n"
                "              <SHORT-NAME>Word</SHORT-NAME>\n"
                "              <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
                f"              <BASE-TYPE-SIZE>{size}</BASE-TYPE-SIZE>\n"
                "              <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
                "              <NATIVE-DECLARATION>uint16</NATIVE-DECLARATION>\n"
                "            </SW-BASE-TYPE>\n"
            ).replace("PACKAGE", package)

        def impl(name: str, tree: str) -> str:
            return (
                "            <IMPLEMENTATION-DATA-TYPE>\n"
                f"              <SHORT-NAME>{name}</SHORT-NAME>\n"
                "              <CATEGORY>VALUE</CATEGORY>\n"
                "              <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS>\n"
                "              <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                f'                <BASE-TYPE-REF DEST="SW-BASE-TYPE">{tree}/Word</BASE-TYPE-REF>\n'
                "              </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                "              </SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>\n"
                "            </IMPLEMENTATION-DATA-TYPE>\n"
            )

        def unit(name: str, display: str) -> str:
            return (
                "            <UNIT>\n"
                f"              <SHORT-NAME>{name}</SHORT-NAME>\n"
                f"              <DISPLAY-NAME>{display}</DISPLAY-NAME>\n"
                "            </UNIT>\n"
            )

        def primitive(name: str, unit_ref: str | None) -> str:
            props = (
                ""
                if unit_ref is None
                else (
                    "                <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS>\n"
                    "                <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                    f'                  <UNIT-REF DEST="UNIT">{unit_ref}</UNIT-REF>\n'
                    "                </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
                    "                </SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>\n"
                )
            )
            return (
                "              <APPLICATION-PRIMITIVE-DATA-TYPE>\n"
                f"                <SHORT-NAME>{name}</SHORT-NAME><CATEGORY>VALUE</CATEGORY>\n"
                f"{props}"
                "              </APPLICATION-PRIMITIVE-DATA-TYPE>\n"
            )

        def data_type_map(application: str, implementation: str) -> str:
            return (
                "                <DATA-TYPE-MAP>\n"
                '                  <APPLICATION-DATA-TYPE-REF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
                f"{application}</APPLICATION-DATA-TYPE-REF>\n"
                '                  <IMPLEMENTATION-DATA-TYPE-REF DEST="IMPLEMENTATION-DATA-TYPE">'
                f"{implementation}</IMPLEMENTATION-DATA-TYPE-REF>\n"
                "                </DATA-TYPE-MAP>\n"
            )

        # Axis 1: bare collapses onto the twin; a UNIT-REF keeps both elements.
        own_unit = None if bare else "/Acme/TreeT/Types/Scale"
        # Axis 2: how the SECOND declaration differs follows axis 1, because the
        # property that is visible differs with it.
        if identical:
            alt_unit = own_unit
            alt_map = "/Acme/TreeT/Types/Ratio"
        elif bare:
            alt_unit = None
            alt_map = "/Acme/TreeF/Types/RawGauge"
        else:
            alt_unit = "/Acme/TreeF/Types/Scale"
            alt_map = "/Acme/TreeT/Types/Ratio"
        # Axis 3: the second map's target, inside the selected package or not.
        gauge_target = "/Acme/App/Types/RawGauge" if inside else "/Acme/TreeF/Types/RawGauge"

        twin_tree = (
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>TreeT</SHORT-NAME>\n"
            "          <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME>\n"
            "          <ELEMENTS>\n"
            f"{base('TreeT', 16)}{impl('Ratio', '/Acme/TreeT/Types')}{unit('Scale', 'pct')}"
            "          </ELEMENTS>\n"
            "          </AR-PACKAGE></AR-PACKAGES>\n"
            "        </AR-PACKAGE>\n"
        )
        other_tree = (
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>TreeF</SHORT-NAME>\n"
            "          <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME>\n"
            "          <ELEMENTS>\n"
            f"{base('TreeF', 32)}{impl('RawGauge', '/Acme/TreeF/Types')}{unit('Scale', 'permille')}"
            "          </ELEMENTS>\n"
            "          </AR-PACKAGE></AR-PACKAGES>\n"
            "        </AR-PACKAGE>\n"
        )
        alt_tree = (
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>Alt</SHORT-NAME>\n"
            "          <AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME><ELEMENTS>\n"
            f"{primitive('Ratio', alt_unit)}"
            "          </ELEMENTS></AR-PACKAGE></AR-PACKAGES>\n"
            "        </AR-PACKAGE>\n"
        )
        app_types = (
            "            <AR-PACKAGE><SHORT-NAME>Types</SHORT-NAME><ELEMENTS>\n"
            f"{primitive('Ratio', own_unit)}{primitive('Gauge', None)}"
            "              <IMPLEMENTATION-DATA-TYPE>\n"
            "                <SHORT-NAME>RawGauge</SHORT-NAME>\n"
            "                <CATEGORY>VALUE</CATEGORY>\n"
            "                <SW-DATA-DEF-PROPS><SW-DATA-DEF-PROPS-VARIANTS>\n"
            "                <SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            '                  <BASE-TYPE-REF DEST="SW-BASE-TYPE">/Acme/App/Types/Byte</BASE-TYPE-REF>\n'
            "                </SW-DATA-DEF-PROPS-CONDITIONAL>\n"
            "                </SW-DATA-DEF-PROPS-VARIANTS></SW-DATA-DEF-PROPS>\n"
            "              </IMPLEMENTATION-DATA-TYPE>\n"
            "              <SW-BASE-TYPE>\n"
            "                <SHORT-NAME>Byte</SHORT-NAME>\n"
            "                <CATEGORY>FIXED_LENGTH</CATEGORY>\n"
            "                <BASE-TYPE-SIZE>8</BASE-TYPE-SIZE>\n"
            "                <BASE-TYPE-ENCODING>NONE</BASE-TYPE-ENCODING>\n"
            "                <NATIVE-DECLARATION>uint8</NATIVE-DECLARATION>\n"
            "              </SW-BASE-TYPE>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
        )
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<AUTOSAR xmlns="http://autosar.org/schema/r4.0" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_4-2-2.xsd">\n'
            "  <AR-PACKAGES>\n"
            "    <AR-PACKAGE>\n"
            "      <SHORT-NAME>Acme</SHORT-NAME>\n"
            "      <AR-PACKAGES>\n"
            f"{twin_tree}{other_tree}{alt_tree}"
            "        <AR-PACKAGE>\n"
            "          <SHORT-NAME>App</SHORT-NAME>\n"
            "          <AR-PACKAGES>\n"
            f"{app_types}"
            "            <AR-PACKAGE><SHORT-NAME>Maps</SHORT-NAME><ELEMENTS>\n"
            "              <DATA-TYPE-MAPPING-SET><SHORT-NAME>Set</SHORT-NAME><DATA-TYPE-MAPS>\n"
            f"{data_type_map('/Acme/App/Types/Ratio', '/Acme/TreeT/Types/Ratio')}"
            f"{data_type_map('/Acme/App/Types/Gauge', gauge_target)}"
            f"{data_type_map('/Acme/Alt/Types/Ratio', alt_map)}"
            "              </DATA-TYPE-MAPS></DATA-TYPE-MAPPING-SET>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
            "            <AR-PACKAGE><SHORT-NAME>Interfaces</SHORT-NAME><ELEMENTS>\n"
            "              <SENDER-RECEIVER-INTERFACE><SHORT-NAME>IRatio</SHORT-NAME>\n"
            "                <IS-SERVICE>false</IS-SERVICE>\n"
            "                <DATA-ELEMENTS><VARIABLE-DATA-PROTOTYPE><SHORT-NAME>value</SHORT-NAME>\n"
            '                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            "/Acme/App/Types/Ratio</TYPE-TREF>\n"
            "                </VARIABLE-DATA-PROTOTYPE>\n"
            "                <VARIABLE-DATA-PROTOTYPE><SHORT-NAME>gauge</SHORT-NAME>\n"
            '                  <TYPE-TREF DEST="APPLICATION-PRIMITIVE-DATA-TYPE">'
            "/Acme/App/Types/Gauge</TYPE-TREF>\n"
            "                </VARIABLE-DATA-PROTOTYPE></DATA-ELEMENTS>\n"
            "              </SENDER-RECEIVER-INTERFACE>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
            "            <AR-PACKAGE><SHORT-NAME>Components</SHORT-NAME><ELEMENTS>\n"
            "              <APPLICATION-SW-COMPONENT-TYPE><SHORT-NAME>Sensor</SHORT-NAME><PORTS>\n"
            "                <P-PORT-PROTOTYPE><SHORT-NAME>pRatio</SHORT-NAME>\n"
            '                  <PROVIDED-INTERFACE-TREF DEST="SENDER-RECEIVER-INTERFACE">'
            "/Acme/App/Interfaces/IRatio</PROVIDED-INTERFACE-TREF>\n"
            "                </P-PORT-PROTOTYPE>\n"
            "              </PORTS>\n"
            "              <INTERNAL-BEHAVIORS><SWC-INTERNAL-BEHAVIOR>\n"
            "                <SHORT-NAME>SensorBehavior</SHORT-NAME>\n"
            "                <DATA-TYPE-MAPPING-REFS>\n"
            '                  <DATA-TYPE-MAPPING-REF DEST="DATA-TYPE-MAPPING-SET">'
            "/Acme/App/Maps/Set</DATA-TYPE-MAPPING-REF>\n"
            "                </DATA-TYPE-MAPPING-REFS>\n"
            "              </SWC-INTERNAL-BEHAVIOR></INTERNAL-BEHAVIORS>\n"
            "              </APPLICATION-SW-COMPONENT-TYPE>\n"
            "            </ELEMENTS></AR-PACKAGE>\n"
            "          </AR-PACKAGES>\n"
            "        </AR-PACKAGE>\n"
            "      </AR-PACKAGES>\n"
            "    </AR-PACKAGE>\n"
            "  </AR-PACKAGES>\n"
            "</AUTOSAR>\n"
        )

    @staticmethod
    def _pair_answer(kind: str, real):
        """One candidate answer for the pair's edge, in ``_walked_chain``'s signature."""

        def walked(groups, decisions, package_selected, twin, fallback, *, certain, certain_set):
            if kind == "actual":
                return real(
                    groups, decisions, package_selected, twin, fallback, certain=certain, certain_set=certain_set
                )
            if kind == "twin":
                return (twin,)
            return ()

        return walked

    @staticmethod
    def _set_answer(kind: str, real):
        """One candidate answer for the set's edge, in ``_mapping_set_targets``' signature."""

        def targets(mapping_set):
            if kind == "actual":
                return real(mapping_set)
            if kind == "both-sides":
                return tuple(
                    reference
                    for entry in mapping_set.maps
                    for reference in (entry.application_ref, entry.implementation_ref)
                )
            if kind == "application":
                return tuple(entry.application_ref for entry in mapping_set.maps)
            return tuple(entry.implementation_ref for entry in mapping_set.maps)

        return targets

    def test_no_alternative_answer_keeps_strictly_more_than_the_ones_the_walk_gives(
        self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No alternative dominates the walk's answers, but on the rows of :data:`KNOWN_DOMINATED`; both seams are live."""
        from swcraft_tools.arxml_import import merge as merge_module
        from swcraft_tools.arxml_import import report as report_module
        from swcraft_tools.arxml_import.report import in_selected_package

        real_pair = merge_module._walked_chain
        real_set = report_module._mapping_set_targets
        root = tmp_path_factory.mktemp("mapping-set-differential")
        manifest = str(_init_project(root, "project") / "swcraft.yaml")
        source = root / "shape.arxml"
        selections = (("App",), ("App", "TreeT"), ("App", "TreeF"), ("App", "TreeT", "TreeF"))

        dominated: dict[str, list[str]] = {}
        pair_answers_that_differ = 0
        set_answers_that_differ = 0
        rows_where_the_pair_seam_is_live = 0
        rows_where_the_set_seam_is_live = 0
        for bare, identical, inside in itertools.product((True, False), repeat=3):
            source.write_text(self._shape(bare=bare, identical=identical, inside=inside), encoding="utf-8")
            for packages in selections:
                losses: dict[tuple[str, str], tuple[frozenset[str], frozenset[tuple[str, str]]]] = {}
                for pair_kind in self.PAIR_ANSWERS:
                    for set_kind in self.SET_ANSWERS:
                        monkeypatch.setattr(merge_module, "_walked_chain", self._pair_answer(pair_kind, real_pair))
                        monkeypatch.setattr(report_module, "_mapping_set_targets", self._set_answer(set_kind, real_set))
                        report = run_import(str(source), manifest, packages=list(packages), dry_run=True)
                        losses[(pair_kind, set_kind)] = (
                            frozenset(
                                entry.path
                                for entry in report.skip_entries
                                if in_selected_package(entry.path, set(packages))
                            ),
                            frozenset((record.target, record.cause) for record in report.unresolved_references),
                        )
                monkeypatch.setattr(merge_module, "_walked_chain", real_pair)
                monkeypatch.setattr(report_module, "_mapping_set_targets", real_set)

                actual = losses[("actual", "actual")]
                row = "/".join(
                    (
                        "bare" if bare else "physics",
                        "identical" if identical else "differing",
                        "inside" if inside else "outside",
                        "+".join(packages),
                    )
                )
                better = [
                    f"{pair_kind}&{set_kind}"
                    for pair_kind, set_kind in losses
                    if (pair_kind, set_kind) != ("actual", "actual")
                    and losses[(pair_kind, set_kind)][0] <= actual[0]
                    and losses[(pair_kind, set_kind)][1] <= actual[1]
                    and (losses[(pair_kind, set_kind)][0] < actual[0] or losses[(pair_kind, set_kind)][1] < actual[1])
                ]
                if better:
                    dominated[row] = better
                # Recorded PER ROW: an aggregate is satisfied by a seam live on ONE row.
                pair_here = len({losses[(kind, "actual")] for kind in self.PAIR_ANSWERS}) - 1
                set_here = len({losses[("actual", kind)] for kind in self.SET_ANSWERS}) - 1
                pair_answers_that_differ += pair_here
                set_answers_that_differ += set_here
                rows_where_the_pair_seam_is_live += 1 if pair_here else 0
                rows_where_the_set_seam_is_live += 1 if set_here else 0

        assert pair_answers_that_differ > 0
        assert set_answers_that_differ > 0
        # The measured minima, so a seam live on fewer rows is red rather than
        # absorbed by the totals above.
        assert rows_where_the_pair_seam_is_live >= self.MINIMUM_LIVE_PAIR_ROWS
        assert rows_where_the_set_seam_is_live >= self.MINIMUM_LIVE_SET_ROWS
        assert set(dominated) == self.KNOWN_DOMINATED, dominated
