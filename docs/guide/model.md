<!-- draft: not yet reviewed by the product's author -->

# Working with the model

<!-- purpose: Writing and changing the model's files: how they divide the architecture and refer to each other -->

The model is a directory of YAML files. This page is about how they divide up the
architecture, how they point at one another, and what happens when you move or rename one.
Every key each file accepts is on the [reference pages](reference/model/index.md), which are
generated from SWCraft's own definitions; this page is the part no generator knows.

## One element per file { #one-element-per-file }

<!-- purpose: The one-element-per-file rule: how a file is named, and what identifies an element -->

The rule everything else follows: **one element per file, the file named after the element —
and the name inside the file, never the file's own name, is what identifies it.**

```text
model/
  swc/sensing/SpeedSensor.yaml      # a component named SpeedSensor, in group "sensing"
  interfaces/IVehicleSpeed.yaml     # an interface named IVehicleSpeed
  requirements/REQ_SPEED_001.yaml   # a requirement whose id is REQ_SPEED_001
```

There is no file that lists the others. The directory is the list, and that has three
consequences worth expecting before you meet them:

- **A name is an identity, not a label.** Renaming an element is a delete and a create, not
  an edit, because every reference to it points at its name. Every reference to the old name
  has to move at the same time, which is what `swcraft impact` is for.
- **Two people adding two elements never conflict.** Two new files are two new files. This
  is most of why the model merges like source code.
- **Requirements are the exception to the naming rule, and only in which key.** A
  requirement is identified by `id`, so its file is `REQ_SPEED_001.yaml` and the key inside
  is `id:`, not `name:`. Everything else in the model is `name:`.

**Two names that differ only in case are two elements, and they get two files.** `float32`
and `Float32` are different names, but macOS and Windows hold `float32.yaml` and
`Float32.yaml` as one file. So a file name is compared without regard to case before
anything is written, on every operating system, and an element whose file name another
element's file already holds gets the nearest free `~1`, `~2`, … on its file instead:
`float32.yaml` beside `Float32~1.yaml`. Nothing else moves — the `name:` inside, every
reference to the element and everything SWCraft generates are exactly what they would be
without the collision, and an import names each such file in its report. The same holds
for a file you name yourself: an element is found by the name inside its file, so you may
drop a `~1` from a file name once no other file holds the plain name (compared without regard
to case, as always), and nothing SWCraft generates changes — elements are read and written
out in an order decided by their names, never by what their files are called. Nothing drops a suffix for you. The comparison is by case alone: two names that differ only in how an
accented letter is encoded are not caught by it.

`swcraft check-model` reports a tree that cannot survive a move between machines: two
files in one layer directory whose names differ only in case, which only a case-sensitive
filesystem can hold and a checkout on macOS or Windows would silently turn into one.
Rename one of the two, and do it before asking the assistant for anything else: while such a
pair exists it refuses every proposal, because each proposal is checked against every error finding.
Two component group directories whose names differ only in case cannot survive the move either,
and nothing reports them.

Each layer is a directory under the model root — `model/` by default, and whatever your
manifest's `model_root` says otherwise. Nothing but the manifest ever refers to that path,
so a project is free to call it something else. The seven layers that describe data share one
directory, `data_types/`, each in a sub-directory of its own (below); every other layer is a
directory directly under the model root.

**An element file directly in `data_types/`, or in a `mapping_sets/` directory at the model root,
is not read — the whole model is refused.** That is where data types and mapping sets used to live,
and a model that still keeps a file there is refused by every command that reads it, with a sentence
naming the directories and the layers the files belong in. Nothing moves a file for you: move each
one into the directory of its layer. The [upgrading page](upgrading.md) has the recipe.

## The layers, and what each one is for { #the-layers-and-what-each-one-is-for }

<!-- purpose: The fifteen layers in three groups, and what each layer is for -->

There are fifteen. It helps to meet them in three groups rather than alphabetically, because
that is the order you actually fill them in. Whatever the layer, an element's `description` that
reaches the AUTOSAR file is written into it as that element's description, exactly as you wrote it.

### What the software is { #what-the-software-is }

<!-- purpose: Writing the software: the data types, interfaces, components and connectors -->

This group is the part that reaches AUTOSAR, and it is where a first model starts.

**Data types** are the vocabulary, and they are seven layers, each a directory under `data_types/`:

| Directory | Layer | What it holds |
|---|---|---|
| `data_types/application/` | [application types](reference/model/application_types.md) | the physical view: an `application_type` (how a signal is measured), an `enum`, a `record`, an `array` |
| `data_types/implementation/` | [implementation types](reference/model/implementation_types.md) | what generated code stores a value in: a `value_type`, a `reference_type`, an `implementation_record`, an `implementation_array`, a `pointer`, and an `external_type` (below) |
| `data_types/base/` | [base types](reference/model/base_types.md) | a `base_type`: how the platform stores a value — its size, encoding and C type |
| `data_types/units/` | [units](reference/model/units.md) | a `unit`, a unit of measurement several types can share |
| `data_types/compu_methods/` | [compu methods](reference/model/compu_methods.md) | a conversion several types can share: a `texttable` (named values) or a `linear` one |
| `data_types/constraints/` | [data constraints](reference/model/data_constraints.md) | a `data_constr`, physical limits several types can share |
| `data_types/mapping_sets/` | [mapping sets](reference/model/mapping_sets.md) | a `mapping_set`, the pairing of the two views (below) |

Where a layer holds more than one kind, a `kind:` key tells them apart. **A name is unique within
its layer, not across them**: an application type and an implementation type may share one name,
and so may a base type and the value type resting on it — each is its own element, in its own
directory, and everything that names one says which layer it means.

That is why a data element and an operation argument name their type in one of **two** keys —
`application_type` for the physical view, `implementation_type` for what code stores — and exactly
one of the two:

```yaml
# model/interfaces/IVehicleSpeed.yaml
name: IVehicleSpeed
kind: sender_receiver
data_elements:
  - name: speed
    implementation_type: float32
```

A file that still says `type:` on a data element or an argument is refused when it is read.

Three rules catch people out: a base type is never what a data element, an argument, a record
field or an array element names — a value type names it as its `base`, and a reference type's
`base` names an implementation type; a record field and an array element name the physical view
only — an application type, an enumeration, a record or an array — because AUTOSAR reserves those
positions for it; and no type may, through any chain of other types, contain itself.

**An application type is how a signal is measured.** It carries a `unit` — free text such as
`km/h` — a `conversion`, the two numbers `factor` and `offset` that turn the stored value into
the physical one (`physical = stored × factor + offset`), and a `range`, the physical `min` and
`max` it is declared within. All three are optional, and each one you leave out is one fewer
element in the generated file: a type that declares nothing is just a name and a category. A
`conversion` needs a unit: it is written as a linear conversion, which AUTOSAR requires to name a
unit, so `swcraft check-model` reports a type with a conversion and no unit, and the generator
refuses it. A number you write as an integer — a bound, a factor, an offset — is held and written
as that integer, exactly, never turned into a fraction on the way. A value type's own
`unit` is documentation and reaches no file, and its `value_min`/`value_max` range is written as a
data constraint on the stored value; an application type's three all reach the file as the
physical view, and that is the difference between the two kinds.

**A unit, a conversion or a range can be an element of its own, shared by several types.** Written
on the type, each one is generated for that type alone (`{name}_Unit`, `{name}_Compu`,
`{name}_Range`). Declared once in its own layer, it is written once, and every type that names it
refers to it:

```yaml
# model/data_types/units/KilometrePerHour.yaml
name: KilometrePerHour
display_name: km/h
```

```yaml
# model/data_types/application/VehicleSpeed.yaml
name: VehicleSpeed
kind: application_type
unit_ref: KilometrePerHour
```

An application type names a unit by `unit_ref`, a `linear` compu method by `compu_method_ref` and
a data constraint by `data_constr_ref` — each **instead of** its inline form, `unit`,
`conversion` or `range`, never beside it. A `linear` compu method names its unit by `unit`, and
has a `factor` and an `offset`; a data constraint has a `min`, a `max` and, optionally, a `level`.
An element no type names is written too.

**A conversion can state the stored values it applies to.** An inline `conversion` and a `linear`
compu method may each carry `limits`: a `lower` and an `upper` bound, each a `value` on the stored
side with an `interval` of `closed` (the default — the value itself belongs) or `open`. They are
written as the lower and upper limit of the conversion's scale, which AUTOSAR reads as the values
the conversion is defined for, before it is applied. They are not the type's `range`: the range
says which physical values are valid, the limits say which stored values the conversion may be
applied to at all. Leave a side out for no limit on that side. The lower bound must not be above
the upper one, and the two may be equal only when both are `closed`; a file that says otherwise is
refused when it is read.

```yaml
# model/data_types/compu_methods/SpeedScale.yaml
name: SpeedScale
kind: linear
unit: KilometrePerHour
factor: 0.1
limits:
  lower: {value: 0}
  upper: {value: 4000, interval: closed}
```

**Named values are a table.** An enumeration states its values one of two ways: inline, as
`literals`, from which SWCraft generates the table (`{name}_CompuMethod`), or by naming a declared
`texttable` compu method in `compu_method_ref` — exactly one of the two. A value type or a reference
type may name a `texttable` too, in its own `compu_method_ref`: that is how a value stored as a
number gets names for its values. A table names each entry once, with a `name` and a whole-number
`value`; an entry name or value written twice in one table is reported.

**A structure, an array or a pointer has an implementation side too.** A record and an array are
the physical view; what generated code stores them in is an `implementation_record`, an
`implementation_array` or, for a pointer, a `pointer`:

```yaml
# model/data_types/implementation/MotorCommand_T.yaml
name: MotorCommand_T
kind: implementation_record
elements:
  - name: Left
    type: MotorLevel_T
  - name: Right
    type: MotorLevel_T
```

An implementation record lists its `elements` in order, each with a `name` and a `type`. An
implementation array has an `element_type`, a fixed `length` and an `element_name`, the name its one
element is written under. A pointer has a `target`. Each of them names implementation types only — a
value type, a reference type, another of these three or an external type — never a base type and
never the physical view. A pointer to a pointer goes through a pointer type of its own: `target`
names another `pointer`. A data element or an operation argument may be typed by any of the three,
and a reference type may rest on any of them. What these kinds cannot say: an array whose size
varies, a union, a pointer to a function or to `void`, and a member declared inline rather than by
a named type. A pointer that leads back to itself through other types is reported, as any other
type that contains itself. That is SWCraft's own restriction, not one it attributes to AUTOSAR: a
pointer does not contain what it points to, but SWCraft does not model a type that reaches itself
through one.

**[Mapping sets](reference/model/mapping_sets.md)** say which implementation type stores each
application type. A set is one file in `data_types/mapping_sets/` holding a list of pairs —
`application`, the name of an element of the application types layer, and `implementation`, the
name of a value type, a reference type, an implementation record or an implementation array — and
a component names the sets it uses in `data_type_mappings`. Generated code needs the pairing to know what a physical signal
is stored in, so `swcraft check-model` reports an application type **or an enumeration** a
component's ports speak in that no set the component names pairs — an enum is a physical type
like any other. The directory is required and **may be empty**: a project whose interfaces are
typed by value types and reference types alone needs no set at all.

A set may also pair a **record** with an implementation record and an **array** with an
implementation array. Such a pair is optional — a record or an array no set pairs is not reported —
but a pair you write is checked, **in part**: the two sides must be of matching kinds, a record must
have as many elements as its implementation record and each member must match the element in the
same position in kind and shape, all the way down, and an array must have the same length and a
matching element. A pointer is never paired with anything, directly, through a reference type or
inside a structure a set pairs. What the check does **not** judge, so that nobody reads more into a
clean run:

- **names** — a record whose members are named differently from its implementation record's elements
  is accepted, and an import keeps both sides' names exactly as the source had them;
- whether a number's range or conversion fits the type that stores it;
- two members of the same shape in swapped positions — `{int, int}` against `{int, int}` cannot tell
  which `int` means what;
- a member whose type is external or does not resolve: it is not judged, never called compatible.

`swcraft generate arxml` refuses exactly the pairs `swcraft check-model` reports, and writes
nothing.

**[Interfaces](reference/model/interfaces.md)** are what travels between components. A
`sender_receiver` interface carries typed data elements; a `client_server` interface offers
operations with typed, directional arguments. An interface is declared once and typed by the
data types above.

**An interface or a type can live outside the project.** A real AUTOSAR delivery is split across
files, and an export you import often references an interface or a type that another file of the
same delivery declares. SWCraft records such a target as an **external** element: an
`external_interface` in `interfaces/`, an `external_type` in `data_types/implementation/`. It holds
its name and the reference the source made — `external_ref`, the target's full AUTOSAR path, and
`external_dest`, the kind of element the reference said it points at:

```yaml
# model/interfaces/IBrakeRequest.yaml
name: IBrakeRequest
kind: external_interface
external_ref: /Delivery/Services/Interfaces/IBrakeRequest
external_dest: CLIENT-SERVER-INTERFACE
```

A port may name an external interface, and a data element's or an argument's `implementation_type`,
a reference type's `base`, an implementation record's element, an implementation array's element and
a pointer's `target` may name an external type, exactly as they name any other. The generated file
writes that reference back as the source had it and generates nothing for the external element
itself, so the file is meant to be read **together with** the files that declare the targets. Import
writes these for you, and you may write one by hand.

**An external interface may list the members your ports use** — `operations` on one whose
`external_dest` is `CLIENT-SERVER-INTERFACE`, `data_elements` on a `SENDER-RECEIVER-INTERFACE` one,
names only, never their types or arguments:

```yaml
# model/interfaces/IBrakeRequest.yaml
name: IBrakeRequest
kind: external_interface
external_ref: /Delivery/Services/Interfaces/IBrakeRequest
external_dest: CLIENT-SERVER-INTERFACE
operations:
  - RequestBrake
```

Through a port on it, a runnable may then call, read or write a listed member, an `operation` event
may name a listed operation, and a `com_specs` entry or a `measures` key may name a listed data
element, exactly as through a declared interface. The generated file refers to a listed member as
the external interface's path, then `/`, then the member's name as you wrote it, and writes the
port's communication settings from the list as it would for a declared interface. Each name may be
listed once, compared without regard to case; any other external interface — a mode-switch one, for
instance — lists nothing. An import fills the lists for you with every member its constructs name
directly under the external interface's path.

What an external element cannot do is tell SWCraft what is inside it, so SWCraft does not guess:

- a connector or a delegation across an external interface is checked for the **kind** of interface
  its `external_dest` names — a client-server one never satisfies a sender-receiver port — and for
  nothing beyond it; where `external_dest` is one of the two general values, `PORT-INTERFACE` or
  `DATA-INTERFACE`, not even the kind is known;
- a listed member is a name and nothing more: its type, its arguments and its queueing are not
  known, so nothing that needs them is judged through it — no signature, queue or initial-value
  check, and no safety level rule reads a `measures` key on such a port. A read, a write, a call, an
  event, a `com_specs` entry or a `measures` key naming a member the list does not hold is reported
  by `swcraft check-model`, naming the external interface and its list;
- a port's `com_specs` entry for a listed data element may not state an `init` — there is no type
  to shape it by — nor, on a `require` port, a `queue_length`: such a member is never queued;
- an initial value cannot be given to a data element whose type is external — `swcraft check-model`
  reports it and the generator refuses it;
- a record's field, an array's element (the physical view; an implementation record's or array's
  element may) and a mapping set never name an external type, and nothing is ever external about an
  application type;
- a component whose files reach an external element cannot be exported on its own as a component
  file, and `swcraft import component` does not replace an external element with the definition a
  component file brings: a file declaring an interface or a type of that name is reported as a
  conflict. A port of a component file whose interface neither the file nor your project declares
  is written on an external interface — yours, when you already hold one for that same path, or a
  new one — and the component's runnables are then left out, because nothing the external interface
  lists was declared by anyone ([Command line](cli.md#what-the-project-already-has) has the detail).

`swcraft check-model` reports an external element whose `external_ref` is blank or not an AUTOSAR
path, or whose `external_dest` is not one the AUTOSAR schema allows there — for an interface, one of
the eight port interface kinds; for a type, `IMPLEMENTATION-DATA-TYPE`. It also reports an
`external_ref` that starts with `/DataTypes`, `/PortInterfaces` or `/ComponentTypes`, the packages the
generated ARXML declares your own elements in: there the reference would find one of your elements
instead of the other file's. The same holds for an external component. It cannot check that the other
file really holds the target, or that the target is what `external_dest` says: that is for whoever
puts the delivery together.

A data element may also declare an `init` — the value it starts from before anything writes to
it. It is optional, and its shape follows the element's own declared type: a plain number for an
application type, a value type or a reference type, the literal's own **name** for an enumeration,
the **name** of one of the table's entries for a value type or a reference type that names a
`texttable` (itself or further along its chain), a map of exactly the declared fields for a record
or of exactly the declared elements for an implementation record, a list of exactly the declared
length for an array or an implementation array — and no value at all for a pointer. A member whose
type is external cannot take one either.
`swcraft check-model` is what holds the value to the type; the editor cannot, because the type is
declared in another file. The generated AUTOSAR file **carries** the value: each one becomes a
constant of its own, referenced from the data element and from every port that uses it, so a
generator reading the export sees what the signal starts from. The value is written in the form its
type asks for: an element's own value under an application type as a physical value, with the
type's unit where the type has one; an enumeration's literal, and a table entry, as the name you
wrote; any other number — a member of a record or an array among them — as a plain number; a record
or an array member by member. A model that declares no initial
values generates exactly the file it generated before.

Two things worth knowing before you write one. Where the type carries a number, the value has to be
a number: text is refused, so a value that only *looks* like a number — `'010'` in quotes — is
reported rather than exported, no number is ever read as a literal or a table entry, and an integer
too wide for this machine to write out in full is reported too. Where the type carries a table, the
value is an entry's name and nothing else. (`swcraft import model` reads a number it finds in an
imported file through the type's table, as [Command line](cli.md) describes; what it writes into the
model is the entry's name.) And the constant's name is built from the interface's
name and the element's, so the two together have a length limit of 122 characters;
`swcraft check-model` names both of them when a pair is over it.

**A port may start from a value of its own.** A component port's `com_specs` entry for a data
element may state an `init`, held to the element's type exactly as the element's own is. On that
port it takes precedence over the element's `init`; an entry that states none starts from the
element's, as before. The generated file writes the port's value in place, in that port's
communication settings, and leaves the element's own value and constant where they were. Only a
component's ports state one — a composition's port has no `com_specs` — and a queued element takes
none. One thing to expect if you exchange component files: a port value equal to its element's own
is written, because the model states it, but an import reads a port value equal to the element's as
stating none, so importing SWCraft's own export of such a component reports a difference.

**[Components](reference/model/swc.md)** are the software components themselves. A component
declares **ports** — each one `provide` or `require`, each typed by a declared interface (the one
exception, a component declared outside the project, is under compositions below) —
and **runnables**, the schedulable functions, each with the events that trigger it (a
periodic `timing` event, the start-up `init` event, an `operation` event, a `data` event when a value
arrives, or several) and, optionally, what it touches
through the component's own ports — the data elements it `reads` and `writes` and the
operations it `calls`, each entry naming a port and a member of that port's interface. The
lists say what is accessed, not in what order; a sequence owns the order.

Those lists are not documentation. The generated AUTOSAR file carries one **access point** per
entry, under the runnable itself, and that is what tells generated code which signals a runnable
may read, write or call — a runnable that declares nothing gets no access point and no API. A read
can be marked `by: value`, which selects the other of the two ways AUTOSAR hands a value to a
runnable, and that choice is carried through to the file rather than flattened.

**A read or a write can be marked `by: implicit`.** The runnable then works on a copy of the value
that does not change while it runs, and what it writes is published as one copy, rather than each
access going to the port as it happens:

```yaml
runnables:
  - name: ComputeBrakeTorque
    events: [{type: timing, period: 0.01}]
    reads:
      - port: rWheelSpeed
        element: speed
        by: implicit
    writes:
      - port: pBrakeTorque
        element: torque
        by: implicit
```

`by` is one choice per entry: a read is by argument (no `by`), `by: value` or `by: implicit`, and a
write is a send (no `by`) or `by: implicit`. One port and element is read in one form by a runnable,
so a runnable cannot read one element both implicitly and by argument. The generated file writes each
entry in the AUTOSAR container its form selects. `swcraft check-model` reports an implicit read of a
queued element — implicit communication is for the latest value — and an implicit read of an element
the port's `com_specs` entry states `enable_update: true` for, and `swcraft generate arxml` refuses
both; an implicit write of a queued element is not reported, because no rule forbids it.
`swcraft resource-report` counts the implicit reads and writes per task ([Command
line](cli.md#the-reports-implicit-access-section)).

**What `by: implicit` does not say**, so that nobody reads more into it: when the RTE makes and
publishes the copy, and what the copy costs in memory or time — that is the RTE's configuration, and
the model holds no data sizes; whether two runnables share one copy; and an access's scope, which
the model does not hold. A clean `check-model` says nothing about whether an RTE generator accepts
the implicit accesses.

**A runnable may state its `symbol`**, the C name of the function generated code calls for it.
Left out, the runnable's name is its symbol, as before. A stated symbol must be a C identifier — a
letter or an underscore first, then letters, digits and underscores, with no length limit — and no
two runnables of one component may end up with the same symbol, stated or taken from the name;
`swcraft check-model` reports both, and the generator refuses the model before it writes anything.
Uniqueness is judged within one component only: two components whose runnables share a symbol pass,
because what makes a symbol unique on an ECU lies outside the model. A stated symbol equal to the
runnable's own name says the same thing as stating none, and SWCraft's own export of it reads back
as stating none, so `swcraft import component` reports that round trip as a difference.

**A runnable may declare an execution-time budget, `wcet`, with its `wcet_type`.** The budget is
what you allot to one execution of the runnable — an upper bound the model is checked against, never
a measurement. It is a whole number followed by its unit, one of `ns`, `us`, `ms` or `s`, and the
unit is part of the value:

```yaml
runnables:
  - name: Run_Filter
    wcet: 850ns
    wcet_type: gross
```

No space, no leading zero, no decimal point — write `1500us`, not `1.5ms` (ARXML writes a budget as a
whole number of a unit) — at most 18 digits, and above zero; `swcraft check-model` refuses anything
else with a sentence that names the spelling to write. `wcet_type` says what the budget covers:
`gross` includes the calls the runnable makes outside itself, `net` leaves them out; neither counts
interruptions, which are response time and a different question. The two are declared together: a
`wcet` without a `wcet_type` is refused, and so is a `wcet_type` without a `wcet`. When you set a
budget through the assistant, both arrive in one change to the runnable — two separate changes, one
per field, would leave the runnable half-declared in between and are refused. The same holds for
removing a budget: `wcet` and `wcet_type` go in one change. Changing only the number, with the type
kept, is a single change to `wcet` and needs nothing more.

The runnables a task runs may not declare more time between them than the task's own `wcet`;
`swcraft check-model` reports the task, the sum and each runnable's budget as you wrote them. `gross`
and `net` budgets are added alike, and when a `net` one is in the sum the finding says so, because the
calls it leaves out are counted nowhere. The budget **is part of the generated ARXML**: each component
with a budgeted runnable gets one timing description, `<component>_Timing`, holding one execution-time
constraint `<runnable>_Budget` per budgeted runnable, with the type and the number and unit exactly as
you wrote them; `swcraft import model` and `swcraft import component` read it back. [Experiments](experiments.md)
reads it too. A file that still spells the budget `wcet_us` is refused, never converted: write
`wcet: 400us` for `wcet_us: 400` and add the `wcet_type`.

A **server** runnable — the one that answers a client's call — is started by an `operation` event,
which names one of the component's own `provide` ports and an operation of that port's
client-server interface:

```yaml
runnables:
  - name: ServeDiagnosticRecords
    concurrent: true
    events:
      - type: operation
        port: S_DiagnosticRecords
        operation: GetDiagnosticRecord
```

One runnable may serve several operations, one event each. `concurrent: true` says the runnable may
be started again while it is still running; it is left out of the file while it is `false`, and the
generated AUTOSAR file carries it either way. The generated file writes each operation event with
both of its references, so a generator reading the export sees which runnable serves which
operation.

`swcraft check-model` reports an operation event whose port the component does not declare, whose
port is a `require` port or names a sender-receiver interface, or whose operation that interface does
not declare — through a port on an external interface, an operation the external interface does not
list. It also reports one port and operation served twice, by two
runnables or twice on one, and a runnable whose operations could not share one entry point: a
different number of arguments, an argument of another direction in the same position, or argument
types whose structure differs. Types are compared after their references are resolved, and an
application type or an enumeration through the one implementation type the component's mapping
sets pair it with. `swcraft generate arxml` refuses every one of them and writes nothing.

**What that check does not judge**, so that nobody reads more into a clean run:

- the operations' possible errors and their return value, how each argument is passed, and argument
  values a port defines — and an argument position it cannot resolve completely, such as an
  application type the mapping sets pair with no implementation type or with two, is not compared
  at all rather than called compatible;
- a provided operation that no runnable serves;
- an operation event beside a `timing` or an `init` event on one runnable: the RTE specification
  describes mixing as unsupported in explanatory text, with no tagged requirement; SWCraft does not
  judge it. Such a runnable is read, checked, imported and generated as you wrote it, and a clean
  check says nothing about whether an RTE generator accepts it;
- the queue length of a provided operation, which the generated file always writes as 1.

**A runnable can be started when a value arrives**, by a `data` event that names one of the
component's own `require` ports and a data element of that port's sender-receiver interface:

```yaml
runnables:
  - name: FilterSpeed
    events:
      - type: data
        port: R_WheelSpeed
        element: FrontLeft
    reads:
      - port: R_WheelSpeed
        element: FrontLeft
  - name: MonitorSpeed
    events:
      - type: timing
        period: 0.1
      - type: data
        port: R_WheelSpeed
        element: FrontRight
        disabled_modes:
          - port: R_EcuMode
            mode_group_ref: /ModeDeclarationGroups/EcuMode
            mode_ref: /ModeDeclarationGroups/EcuMode/Sleep
```

`FilterSpeed` runs each time a new `FrontLeft` sample arrives. `MonitorSpeed` runs every 100 ms and,
in between, each time a `FrontRight` sample arrives — except in the `Sleep` mode, which its
`disabled_modes` names the way a timing event's does. `port` and `element` are the names a `reads`
entry uses, so the event and the read are written alike. Through a port on an external interface, the
element is one the external interface lists. A runnable may carry several such events, one per
element that starts it, and the event may state a `name`, like every event.

`swcraft check-model` reports a `data` event whose port the component does not declare, whose port is
a `provide` port, whose port's interface is a client-server interface or an external interface that is
not sender-receiver, or whose element that interface does not declare or list — one finding per
event. `swcraft generate arxml` refuses every one of them and writes nothing. The generated file writes
each event under the component's behaviour with its port, its element and its disabled modes, so a
generator reading it sees which data starts which runnable. In VS Code a `data` event's `port:` leads
to the port and its `element:` to the data element, and the outline shows the event beside the
others.

**These are not reported**, because each is legal:

- a `data` event beside a `timing` event on one runnable — the example above, and AUTOSAR's own — or
  beside an `init` or an `operation` event, which SWCraft does not judge;
- two runnables started by one element, and one event written twice on one runnable;
- a runnable started by an element it does not read, such as a runnable that only takes note that a
  value arrived;
- an event on a `require` port that no connector feeds, or on an element no component sends: a
  component can be placed in many compositions, and whether its port is connected is the
  composition's question. The resource report says so for each event, below.

**How often a `data`-started runnable runs is not in the model.** A timing event states its period,
the start-up event runs once and a server runs when its callers call, but data arrives when its sender
sends it, and nothing in the model says how often that is.

**A `data`-started runnable mapped into a task is budgeted as if it ran at most once per activation of
that task.** The task's runnables may not declare more time between them than the task's own `wcet`,
and that sum counts such a runnable's `wcet` once, like any other runnable's. The task's `period` then
stands for the shortest interval between two arrivals you plan for. That is a reading of the model,
not a check: no rule compares the task's period with how often the data arrives, and the period check
for runnables reads timing events only.

**`swcraft resource-report` says where each such runnable's data comes from.** For each `data` event
of each placed occurrence of its component, its `ARRIVALS` section follows the model's connectors and
delegations back to the runnables that write the element and the tasks they run in, and prints
either a bound — *data arrives at most every 10 ms (from …)*, the shortest period of a sending task —
or why the source is outside the model: no connector reaches the port, the port is delegated out of
the top composition, the sender is an external component, no runnable of the sender writes the
element, or a writer runs in no task. The figure is **a bound taken from the senders' task periods**,
derived from the model; it is not a measured rate and it says nothing about response time. The
section is a report: it adds no finding, and the command still succeeds. [Command line](cli.md#the-reports-arrivals-section)
shows the section.

The [allocation experiment](experiments.md) never moves and never costs such a runnable, because the
model does not hold its rate.

**What the `data` events do not cover**, so that nobody reads more into a clean run: no arrival rate,
no response time and no end-to-end latency; no check that two runnables started by one queued element
run in one task — an RTE generator of a later AUTOSAR release refuses that case, and SWCraft adds the
check only when it models queued communication; no wait points, so a runnable cannot wait for the
data; no reason of activation; no event through a port that both provides and requires; and none of
the other three data events AUTOSAR defines, for a reception error, a completed send and a completed
write. A clean `check-model` says nothing about whether an RTE generator accepts the model. No diagram
draws a `data` event; `swcraft diff` and the Change Report list an added or removed one like any other
event.

**A call can be asynchronous.** `asynchronous: true` on a `calls` entry says the runnable does not
wait for the server: the call returns at once, and the result is fetched later, in one of two ways:

```yaml
runnables:
  - name: StoreCalibration
    events: [{type: timing, period: 0.1}]
    calls:
      - port: rNvStorage
        operation: WriteBlock
        asynchronous: true
        timeout: 0.5
      - port: rCalibration
        operation: ReadBlock
        asynchronous: true
        poll: true
  - name: OnBlockWritten
    events:
      - type: call_returned
        port: rNvStorage
        operation: WriteBlock
```

- **`poll: true`** on the call: the calling runnable fetches the result itself, without waiting, the
  next time it runs. Only the runnable that makes a call can poll it.
- **A `call_returned` event** on the runnable that takes the result: it starts that runnable when the
  call of its `port` and `operation` returns. The runnable need not make the call itself; the call is
  the component's one asynchronous call of that port and operation.

Both write a *result point* in the generated file, named `Result_<port>_<operation>` unless you state
its name as `result` — on the call for a poll, on the event for an event; an event's own name is
`ASRT_<port>_<operation>` unless you state `name`. `timeout`, in seconds, is the time after which the
call gives up with an error; it is optional, and written only when you state it. Without it the file
carries no timeout for that call: the file is valid against the AUTOSAR schema, which lets a file leave
it out, but the standard's model of a call asks for one, so such a file is partial rather than
complete — SWCraft writes no value you did not give. A synchronous call is written with a timeout of 0,
which means no timeout monitoring, as before. `poll`, `result` and `timeout` on a call that is not
asynchronous are refused when the file is read, and so are a negative `timeout` and a `result` on a call
that does not state `poll: true`.

`swcraft check-model` reports, and `swcraft generate arxml` refuses:

- one port and operation called both synchronously and asynchronously in one component;
- asynchronous calls of one port and operation, by different runnables, that state different timeouts —
  a call that states no `timeout` differs from one that states any;
- an asynchronous call with no result — no poll and no `call_returned` event — **in a 4.2.2 project
  only**: AUTOSAR 4.2.2 asks every asynchronous call for exactly one result, 4.3.0 for at most one;
- a call with two results, its poll and an event or two events, at either version;
- a `call_returned` event whose port and operation no runnable of the component calls asynchronously,
  and one whose port and operation two runnables call asynchronously, since the event cannot say
  whose call it takes;
- a runnable started by an `init` event that fetches a result, by either form.

The version is the manifest's `arxml_version` (4.2.2 when it states none) for `check-model`, for the
assistant's changes and for `swcraft import component`, and the
version a run writes for `generate arxml`, so `--arxml-version 4.2.2` on a 4.3.0 project refuses an
asynchronous call with no result.

The [allocation experiment](experiments.md) never moves and never costs a runnable that only a
`call_returned` event starts, because the model does not hold when a call returns; [metrics](metrics.md)
count the event's port among the ports such a runnable touches. No diagram draws an asynchronous call
or a `call_returned` event; `swcraft diff` and the Change Report list a changed `by`, `asynchronous`,
`poll`, `result` or `timeout`, and an added or removed `call_returned` event, like any other field and
event.

**What asynchronous calls do not cover**, so that nobody reads more into a clean run: no wait point,
so a runnable cannot block until a call returns — a `call_returned` event always starts its runnable;
no poll of another runnable's call; no disabled modes on a `call_returned` event; when a call
returns, and any response time; and a synchronous call's timeout other than 0. A clean `check-model`
says nothing about whether an RTE generator accepts the calls.

**A component may declare the state its runnables share**, as `variables` — AUTOSAR's
inter-runnable variables — and each runnable names the variables it reads and writes:

```yaml
variables:
  - name: FilteredSpeed
    access: implicit
    application_type: VehicleSpeed
    init: 0
    description: Speed after the 10 ms filter, read by the 100 ms monitor.
  - name: PeakResetRequest
    access: explicit
    implementation_type: Boolean
runnables:
  - name: FilterSpeed
    events: [{type: timing, period: 0.01}]
    writes_variables:
      - variable: FilteredSpeed
  - name: MonitorSpeed
    events: [{type: timing, period: 0.1}]
    reads_variables:
      - variable: FilteredSpeed
    writes_variables:
      - variable: PeakResetRequest
```

Every variable says how it is accessed, and there is no default, because the two give generated code
two different APIs: `explicit` reads and writes the shared value directly, so it may change between
two reads in one run, and `implicit` works on a copy that does not change while the reading runnable
runs. A variable is typed like a data element — exactly one of `application_type` and
`implementation_type` — and may state an `init`, a `description` and a `sw_calibration_access`, with
the same meaning and the same checks as on a data element. A variable belongs to its component: a
runnable may read and write only the variables of its own component, one runnable may both read and
write the same one, and listing a variable twice in one list is refused when the file is read. A
variable nobody reads or writes is fine.

`swcraft check-model` reports an access naming a variable its component does not declare, a type
that is not declared, a variable typed by a pointer — directly, through a reference type, or through
the implementation type the component's mapping sets pair its application type with — and a
variable whose name another name in the component's behaviour already takes, a runnable's or an
event's. `swcraft generate arxml` refuses every one of them and writes nothing. The generated file
writes each variable under the component's behaviour, among the explicit or the implicit ones, with
its initial value written in place, and each read or write under the runnable, named
`RV_{variable}` or `WV_{variable}` unless the entry states a `name` of its own.

**A variable a runnable reads and that states no `init` is a warning.** AUTOSAR leaves its value
undefined when a runnable reads it before any runnable has written it, and the model does not say in
which order runnables run, so SWCraft cannot tell whether a write comes first. The warning is given for
every such read, also where you know the writer runs first; it is not a check of the order, and a run
without the warning says nothing about the order either. Give the variable an `init`, or keep the
warning as a reminder. Like every warning, it leaves `swcraft check-model` and `swcraft validate`
successful unless you pass `--warnings-as-errors`, and the generated file is written without an
initial value.

**What the variables do not cover**, so that nobody reads more into a clean run: nothing checks that
an `explicit` variable that runnables on different tasks share is protected — an exclusive area
(below) can say that two runnables must not interrupt each other, but no check connects it to the
variable — and nothing checks whether an `implicit`
variable's readers and writers run on tasks at all; how a variable is implemented
(`swImplPolicy`) and the scope of an access are not part of the model; an access always names a
whole variable, never a part of it; and the pointer check does not judge an application type that no
mapping set, or more than one, pairs, nor an external type. No diagram draws a variable. The
[cohesion metric](metrics.md) counts two runnables that share a variable as sharing data.

**A component may declare exclusive areas**, and each runnable says which of them it uses. An
exclusive area says that the runnables using it must not interrupt each other while they are inside
it, and a runnable uses one in one of two ways: it **runs inside** the area, so the whole runnable is
guarded, or it **can enter** the area, entering and leaving it by calls in its own code:

```yaml
runnables:
  - name: FilterSpeed
    events: [{type: timing, period: 0.01}]
    runs_inside:
      - area: SpeedState
  - name: MonitorSpeed
    events: [{type: timing, period: 0.1}]
    runs_inside:
      - area: SpeedState
  - name: CalibrateOffsets
    events: [{type: timing, period: 1.0}]
    can_enter:
      - area: OffsetTable
exclusive_areas:
  - name: SpeedState
    description: Guards the filtered speed shared by the 10 ms filter and the 100 ms monitor.
  - name: OffsetTable
```

An area has a `name` and may have a `description`. An area belongs to its component: a runnable may
name only the areas of its own component, in `runs_inside`, in `can_enter`, or in both, and a runnable
may name any number of areas. Listing an area twice in one list, or declaring two areas with one name,
is refused when the file is read. An area no runnable uses is fine, and so is an area only one
runnable uses.

`swcraft check-model` reports a `runs_inside` or `can_enter` entry naming an area its component does
not declare, and an area whose name another name in the component's behaviour already takes — a
runnable's, an event's or a variable's, exactly or differing only in case. An area's name must be a
valid AUTOSAR name, and an external component declares no area. `swcraft generate arxml` refuses every
one of them and writes nothing. The generated file writes each area under the component's behaviour,
with its description, and each runnable's areas under the runnable, in the order you list them; it
adds nothing you did not state. In VS Code an `area:` leads to its area.

**What the exclusive areas do not cover**, so that nobody reads more into a clean run: SWCraft does
not check what an area protects — that the data two runnables share is guarded by an area, or that an
area guards anything; it does not know in which order a runnable enters several areas, so it cannot
find a deadlock; and it does not say which tasks an area can delay, which depends on how the RTE
integrator implements the area and on how long a runnable stays inside it, neither of which is part of
the model. One area in both lists of one runnable, and areas whose runnables all run in one task, are
not reported. An area's runnables stay on one core only because `swcraft check-model` reports one
component instance whose runnables run in two partitions ([Safety](safety.md)); where that check is
not made — a model without exactly one root composition, or with a cycle of compositions — nothing
checks the area's runnables either. The order in which areas may be nested, the API options of
AUTOSAR 4.3.0 for an area and a server call made from within an area are not part of the model. No
diagram, resource report or metric figure is computed from the areas; `swcraft diff` and the Change
Report list an added, removed or changed area or entry like any other member.

**The names SWCraft derives can be stated instead.** A component may state `behavior_name`, the
name of its behaviour in the generated file; each `timing`, `init`, `operation` and `data` event may
state a `name`; and so may each entry of a runnable's `reads`, `writes`, `calls`, `reads_variables` and
`writes_variables`, the name of its access point. A stated name is written into the generated file exactly as it is — no prefix, no number, no
change of case — and everything that refers to it follows. Where nothing is stated the name is
derived as below, and a model that states none of them generates the same file as before. A stated
name equal to the derived one says the same thing as stating none; an import states a name only
where the source's differs from the one SWCraft would derive. External components state no
`behavior_name`: they have no behaviour in the generated file.

A runnable may not take the name an event of its component is written with — the event's stated
`name`, or the one SWCraft derives where it states none: `TMT_{runnable}` for a timing event,
`IT_{runnable}` for the start-up event, `OIT_{port}_{operation}` for an operation event,
`DRT_{port}_{element}` for a `data` event, `ASRT_{port}_{operation}` for a `call_returned` event,
numbered where two derived names would clash. A runnable and its events sit side by side in the generated
file, where AUTOSAR requires every name to be unique ignoring case, so the names are compared
ignoring case and against the numbered names the file really carries. `swcraft check-model` reports
such a runnable; the fix is renaming the runnable, or stating the event's name. Two events written
with one name are reported too, which only a stated name can bring about.

The same rule holds for every name in the generated file: within one *name space* — the nearest
element above a name that has a name of its own — two names may not be equal ignoring case. The
lists a file groups names into are not name spaces: a component's ports share one with the name its
behaviour is written with (its `behavior_name`, or `{component}_InternalBehavior` where it states
none); a behaviour's runnables share one with
its events, its component's variables and its component's exclusive areas; a runnable's reads, writes, calls,
variable reads and writes, and the result points of its polled calls and `call_returned` events share one; so do an interface's data elements or
operations, an operation's arguments, a record's fields or elements, and a composition's ports,
members, delegations and the connectors drawn inside it. `swcraft check-model` reports two names that
differ only in case, and one name used in two lists of one element — a composition port and a member
both called `Brake` — naming the name space and each name with its list; `swcraft generate arxml`
refuses the same. The same name twice in **one** list is the duplicate check that list always had. An
enumeration literal's label, and the requirements, tasks, sequences and hardware, which are not
written to the ARXML, are not compared.

The packages the generated file declares elements in are name spaces too. Each data description layer
has a package of its own under `/DataTypes` — `/DataTypes/Application`, `/DataTypes/Implementation`,
`/DataTypes/Base`, `/DataTypes/Units`, `/DataTypes/CompuMethods`, `/DataTypes/Constraints` and
`/DataTypes/MappingSets` — and a form SWCraft generates for a type (`{name}_Unit`, `{name}_Compu`,
`{name}_Range`, `{name}_CompuMethod`) is written beside that type, in its package. Every interface is
written into `/PortInterfaces`, every component and composition into `/ComponentTypes`. Two names in
one package that differ only in case — a value type `Float32` beside a value type `float32` — are
reported and refused; an application type and an implementation type that differ only in case are
not, because they land in two packages. Within one package, one of the two can say where else to go:

```yaml
name: float32
kind: value_type
base: float32_base
package: Legacy
```

`package` places that element, and the names generated for it, in a sub-package of its layer's
package — here `/DataTypes/Implementation/Legacy/float32` — and every reference to it follows. The
name and every reference in the model stay as they are. An import sets it for you when an export kept
such a pair in two packages of its own, and when it placed an element in a sub-package laid out the
way SWCraft writes one; the value must be a legal AUTOSAR name, one word with no spaces.

Every component must say whether its behaviour can be stopped and restarted, with
`handle_termination_and_restart` — `no_support`, `can_be_terminated` or
`can_be_terminated_and_restarted` (the [component reference](reference/model/swc.md) has the field).
AUTOSAR requires the answer on every component's behaviour, so a component without it is reported by
`swcraft check-model` and refused by `swcraft generate arxml`, naming the three values; SWCraft never
chooses one for you. `no_support` is a statement like the other two — it says the behaviour cannot be
stopped or restarted — and it is written to the generated file as you set it. An external component
has no behaviour in the generated file and may not declare the field.

Components are the one layer with a directory level of its own: `swc/{group}/{name}.yaml`.
The group is the directory, deliberately not a key inside the file, which means moving a
component between groups is a file move and nothing else. Any legal directory name works —
`brake-ecu` is fine. The diagrams do not draw groups: a component appears wherever a composition
places it.

**[Connectors](reference/model/connectors.md)** join components: one file per assembly
connector, from a `provide` port to a `require` port. A connector is a file rather than a key
inside a component so that neither of the two components owns the connection.

The two ends need **not** name the same interface. What is required is that the interface the
requiring port needs is *satisfied by* the one the providing port offers: the same kind of
interface, and every data element — or operation — the requiring side declares has a counterpart
of the same name on the providing side whose type is compatible. The same interface on both ends
always satisfies that, so nothing you have already written changes. Types are compared after
their references are resolved and by structure rather than by name, so two types spelled
differently and built identically are interchangeable; operation arguments are matched by
position and direction, and an argument's own name takes no part. A delegation between a
composition's boundary port and a member's port is judged the same way, with one difference the
AUTOSAR standard itself makes: a `provide` boundary port needs only **one** shared compatible
member, where a `require` one needs them all.

Two implementation records are compared element by element **with their element names**, as two
records always were, and two implementation arrays by length, element type **and the name of their
one element** — here the names do count, unlike in a mapping set, because AUTOSAR's rule for
connecting two structures asks every attribute but the type's own name to match. Two pointers are
compared by what they point to. And a data element or an argument typed by a pointer may not travel
between two **application** components: `swcraft check-model` reports such a connector, followed
through compositions to the components at both ends, and a pointer may cross only where one end is a
service or an ECU-abstraction component. It does not judge a pointer inside a structure, an end on
an external component, or an end no delegation leads to a component.

Two further properties must agree: the service flag, `is_service`, of the two interfaces, and whether
a data element is queued (`sw_impl_policy: queued`) — both sides queued, or neither.

Some things are deliberately **not** compared. Most of them the model does not carry at all:
byte order, memory alignment,
optionality, invalidation policy, the invalid value, the error set a client/server interface may
declare, and the
interface *mappings* the standard offers as another way to make two interfaces fit. Others it does
carry and the comparison passes over by design: a value type's `unit` — documentation
that reaches no generated file — its `value_min`/`value_max` range, which the generated file does
carry as a data constraint, and a data element's `init` and `asil`. Each one is a case SWCraft accepts that a downstream RTE generator
could still reject, so a pair that fits only just is worth a look before you rely on it.

Two **application** types are compared on their physics, because theirs does reach the file: the
unit, the conversion and the range have to match, and a type that declares one of them is not
compatible with one that declares none. Two directions are stricter than the standard and are
worth knowing: two spellings of one physical unit — `km/h` and `kph` — are refused, because the
model carries the text and nothing else to compare; and a provider whose range is *narrower*
than the receiver's is refused, where AUTOSAR would allow it.

### What it runs on { #what-it-runs-on }

<!-- purpose: Writing what the software runs on: tasks, and the processor, cores, partitions and memories -->

**[Tasks](reference/model/tasks.md)** are the OS tasks: a period, a priority, the partition
the task runs in, an optional worst-case execution budget, and the ordered list of runnables the
task executes.
A runnable is named by its component and its own name, because runnable names are only unique
inside their own component. The component is named in **exactly one of two** ways: `swc`, the
component type, or `instance`, the path of composition member names from the root composition down
to one occurrence of that type — `Chassis/left_wheel`, never starting with the root's own name.
Write `swc` while the type is placed once below the root, where it names that one occurrence, and
`instance` once the type is placed more than once, where `swc` names no one occurrence and is
refused:

```yaml
runnables:
  - swc: SpeedSensor                # a type placed once
    runnable: SpeedSensor_Run
  - instance: Chassis/left_wheel    # one occurrence of a type placed twice
    runnable: WheelSpeed_Run
```

**A given runnable of one occurrence is mapped to at most one task** across the whole model,
however each mapping names the occurrence. An `instance` path is not a reference to an element:
`swcraft impact` and `swcraft query --dependents` follow `swc` and never a path, so an entry written
with `instance` is not listed among its component's dependents.

A task may declare a budget too, `wcet`, written the same way (`wcet: 1200us`) and with no
`wcet_type`. Two time formats live side by side here, and both are AUTOSAR's own: `period` is a
number of **seconds**, written `0.01`, the way AUTOSAR writes a timing event's period; a budget is
a whole number with its **unit**, the way AUTOSAR writes a timing constraint. A task's budget is
used by the checks and the reports only and is not written into the ARXML: AUTOSAR's execution-time
constraint names a runnable, never an operating-system task.

**[Hardware](reference/model/hardware.md)** is what the software runs on: exactly one
`processor`, the `core`s it schedules, the `partition`s on those cores, and the `memory`
elements the cores use. Four kinds in one directory, told apart by a `kind:` key, in one flat
namespace — a core, a partition and a memory may not share a name, because a task names its
partition, a partition names its core and a component names its memories by name alone, and one
name has to mean one element.

A **partition** is the group of tasks that run together on one core — AUTOSAR's OS-Application.
Every task names one and takes its core from it, so moving a partition to another core moves all
of its tasks. A project on a single core still declares one partition there, without a level. A
partition may carry a safety integrity level of its own, which [Safety integrity
levels](safety.md#partitions) covers.

**Every project has this layer, and every project declares exactly one processor.** That is
why `swcraft init` does not leave the directory empty: it writes a placeholder processor, one
core beside it and one partition on that core, so a new model loads — and its first task has a
partition to name — before you know which part you are building for. Replace them when you do.

A task's `partition` names a partition here, a partition's `core` names a core here, and the keys
of a component's `memory` map name memories here. All three are checked: a name nothing declares
is a finding, and so is a name that resolves to the wrong kind — a task pointed at a core, a
partition pointed at a memory, or a memory budget keyed by a core.

The budgets live on the element that has the quantity. A core carries a `threshold`, the
share of its time the tasks of its partitions may total. A memory carries a `size` in bytes and a
`threshold` on that size, and every component's budget for it counts against it. Under the
default `enforcement: strict` a missing budget is itself a finding — every task on the core
needs a `wcet`, every component needs an entry for the memory. Setting `enforcement:
advisory` on one core or one memory opts that element out on its own: a missing budget counts
as zero and a breach becomes an information line instead of an error.

`accessible` on a memory names the cores this project allows to use it, and it is a design
rule rather than wiring. Nothing generated from the model configures a memory protection unit;
the integrator is the one who makes the silicon agree with the list. A list of one core is a
memory private to that core.

**The list is checked against the work you put on each core.** Take a task, the core its
partition is on, and the components its runnables belong to: every memory one of those components
charges more than nothing to has to name that core in its `accessible` list. Where it does not,
you get a finding naming the things involved — the task, its partition, the core it runs on, the
component whose budget it is, and the memory that core is not allowed to reach. It has two honest
repairs and SWCraft does not choose between them: admit the core to the memory, or move the
task to a partition on a core the memory already admits. Which one is right is a question about the design,
not about the model file. Where two components on one task budget the same unreachable memory
you get one finding for each of them, because dropping one component's budget is a repair for
that component alone.

**A budget of `0` is not a use.** Under the default `enforcement: strict` a component that
writes no entry for a memory is a finding of its own, so `0` is how you say "this component
uses none of that memory" — and a memory private to one core needs exactly that from every
component that runs elsewhere. Writing it is not a reachability error, and the check starts
asking about that memory the moment the number stops being zero.

What that check *cannot* tell you is whether the silicon agrees. It reads what the project
declared, so it catches a component placed on a core its memory was never meant to serve —
the mistake that is otherwise found on the target. It does not read a linker script, a
protection-unit configuration or a map file, none of which the model expresses at all, so a
memory declared reachable from a core that cannot in fact reach it passes every check here.
The declaration is a statement of intent that the checks hold you to; making the hardware
match it is still the integrator's work. The other two things it does not reach are a
component no task runs — the question is asked of a task's partition's core, and a component nothing maps
has none, so its budgets are checked for naming a declared memory and never for reaching one
— and a task entry naming a runnable its component does not declare, which is reported as the
broken reference it is and not as a topology problem as well.

A core and a memory can also declare the safety integrity level they are qualified to host, and a
partition the level it is; more checks then hold the work you place on them to it — [Safety
integrity levels](safety.md) covers them.

### What it is for, and what it does { #what-it-is-for-and-what-it-does }

<!-- purpose: Writing requirements, compositions and sequences: what the system is for and what it does -->

**[Requirements](reference/model/requirements.md)** carry the intent: an id, a title, a
statement, a `version` and a `source` — `external` for a requirement a requirements tool
delivers, `architecture` for one written here. `swcraft generate needs` publishes the architecture
ones, with the elements that satisfy a requirement, as a Sphinx-Needs file for the requirements team,
and names a delivered one only by its id ([Publishing the architecture to the requirements
team](cli.md#publishing-the-architecture-to-the-requirements-team)). **`source` is what tells the two apart, never the id**: name a
requirement you write here as you like, and SWCraft will not read anything into its name.

**A requirement taken from a requirements tool can say where it came from.** Three optional
fields hold it, each as the tool writes it: `document`, the source document (a name, number or
module); `baseline`, the baseline of that document; and `revision`, the tool's own revision of
the requirement. All three are text — a revision may be `"7"`, `"1.3"` or `"B"` — so quote any
value YAML would read as something else, a number like `7` or a date like `2026-10-18`, or the
file is refused when it is read:

```yaml
# model/requirements/BRK_042.yaml
id: BRK_042
title: Brake request shall be acknowledged within 20 ms
description: The brake controller shall acknowledge every brake request within 20 ms.
req_type: req
version: 2
source: external
document: SRS-Brakes
baseline: "2026-10-18"
revision: "1.3"
```

The two numbers are not the same thing. **`revision` is the revision a reader outside SWCraft
sees**; `version` is SWCraft's own change counter, the number a `satisfies` link names, and it is
never shown in place of `revision`. The three fields are a record and nothing more: no check
reads them, a change to one never moves `version`, and each is written to the file only when it
is set.

**A stub is a placeholder for a parent that is not there yet.** When a requirement you
[import](cli.md#importing-requirements) names a parent in `derived_from` that neither the import nor the model holds, the import writes
the parent as a stub — `stub: true` — so the link has something to point at. A stub is still a
whole requirement, with every field a requirement needs, but its title and description are
placeholders the import writes, not the parent's own text; the import replaces it when the parent
itself arrives. You do not write one by hand, and `stub` takes only `true` or `false` (YAML also reads an unquoted `yes`/`no` or `on`/`off` as these; a quoted `'true'` or a `1` is refused). Until it is replaced, **every stub is reported as a warning** — listed by
`check-model` and `validate`, never a refusal unless you pass `--warnings-as-errors` — and it is
not also reported as a requirement nothing satisfies: one placeholder, one warning. A real
requirement never carries the field.

A requirement names no element. **The element names the requirement**: a component, one of its
ports or runnables, a composition, an interface, a task or a hardware element lists what it
satisfies in `satisfies`, each entry the requirement's id and the version it was written against —
`satisfies: [REQ_SPEED_001@1]`. A link on a port or a runnable counts for the component that owns
it. A requirement that refines another names its parent in `derived_from`, in the same form, or
names the architecture element it came out of as `hardware:CORE0`, `swc:SpeedSensor` and so on.

They are not documentation you may skip. **A requirement nothing satisfies is reported as a
warning** — SWCraft holds that every requirement you write is realized somewhere in the
architecture, and it will tell you when one is not, without holding the file back. A component
that satisfies no requirement is no finding: the link is how a requirement is allocated, not a
duty of every element. A link whose version is not the requirement's current one is
reported as needing confirmation, and one with no version at all as not stated; a link naming a
requirement that does not exist is an error.

**[Compositions](reference/model/compositions.md)** are the structure: a composition has
boundary ports of its own, holds named members that place your components and other
compositions inside it, and delegates its ports through to theirs. Exactly one composition
is the root, and that one is the top of the hierarchy the exported ARXML describes — top in the
sense that everything else is reached through it, not in the sense that the file says so: nothing
in the exported ARXML marks one composition as the root, because AUTOSAR has no such flag at the
component-type level. It is a fact about your model, and it is what a connector that names no
composition belongs to. `init` writes it for
you, empty; every component you wire to another belongs in one, and a connector that names no
composition is drawn inside the root.

A member places a **type**, so the same component may stand in several compositions — once in
each — and the name you give the member need not be the type's. What it may not do is stand
twice in the *same* composition, because then a connector naming that type would have two places
to point at. Budgets and task mappings stay per component **type**, not per placement: a
component placed twice is scheduled once.

**A member's type can live outside the project, too.** When an export you import places a component
whose type another file of the delivery declares, SWCraft writes an **external component**
(`external: true`) holding the type's full path and kind as the source wrote them, and nothing it
cannot know: no runnables, and ports declared by **name and direction** only — the two facts the
connectors that reach it state. Such a port names no `interface`, because the export does not say
which one the other file declares; where every connector on it came from ports typed by one
interface, the import records that name as `observed_interface`:

```yaml
# model/swc/external/Dcm.yaml
name: Dcm
external: true
external_ref: /Delivery/Services/Dcm
external_dest: APPLICATION-SW-COMPONENT-TYPE
ports:
  - name: DataServices_Speed
    direction: require
    observed_interface: DataServices_Speed
  - name: GetCurrentTime
    direction: provide
```

Connectors and delegations to those ports are generated like any other, and the generated file
refers to each port exactly as the export did. The observed interface is an **observation**, not a
declaration: `swcraft query` can filter on it, but no check reads it, and the
[diagrams](diagrams.md) draw such a port as the bare square, since its type is not declared. Do not copy it into `interface` — on an external component's
port `swcraft check-model` reports any `interface` or `measures`, and on every other component's
port a missing `interface` is reported.

What an external component's ports do not give you:

- **no compatibility check** across one: nothing in the model says what the other file's port
  declares, so a connection is checked for direction and nothing else, and the compatibility
  question the assistant can ask answers `ok: true` with a reason beginning *not judged: external
  component* — which is not a statement that the two fit;
- **no safety level** through one: the producer and consumer checks have no signal to compare;
- **no check on the other file**: that its component really has the port, in that direction, is the
  export's claim, carried as it stands.

**[Sequences](reference/model/sequences.md)** are scenarios: an ordered walk through some of
the runnables of **exactly one** task, rendered as a sequence diagram. The steps must be an
ordered subsequence of that task's own `runnables` list — you may leave steps out, but you
may not reorder them, because the task's order is the order they really run in. A step names its
component as a task entry does, by exactly one of `swc` and `instance`, and the diagram labels the
step's lifeline with what the step wrote, so two occurrences of one type stand on two lifelines.
[Diagrams](diagrams.md) is about reading the result.

## How the files find each other { #how-the-files-find-each-other }

<!-- purpose: How elements refer to each other by name, and what happens when you move or rename one -->

Elements refer to each other **by name, never by path**. A port naming `IVehicleSpeed` does
not care which file that interface is in, and moving the file changes nothing.

```text
component, port, runnable, composition, interface, task, hardware --satisfies--> requirement
requirement --derived_from--> requirement, or an element
composition --members[].type--> component
composition --members[].composition--> composition
composition --ports[].interface--> interface
composition --delegations[]--> one of its own ports and one of a member's
connector --composition--> composition (absent means the root)
component --ports[].interface--> interface --application_type--> application type
                                           --implementation_type--> implementation type
value_type --base--> base_type
reference_type --base--> an implementation type
record/array --> an application type      implementation record/array, pointer --> an implementation type
application_type --unit_ref / compu_method_ref / data_constr_ref--> unit / linear / data_constr
enum, value_type, reference_type --compu_method_ref--> texttable
linear --unit--> unit
mapping_set --maps[]--> (application type, implementation type)
component --data_type_mappings[]--> mapping_set
connector --from.swc / to.swc--> component, and .port to one of its ports
task --runnables[]--> (component by swc, or one occurrence by instance; runnable)
runnable --reads[] / writes[] / calls[]--> a port of its own component, and an element
                                           or operation of that port's interface
sequence --task--> task, and --steps[]--> (swc or instance; runnable) of that task
component --memory keys--> memory          task --partition--> partition
partition --core--> core                   memory --accessible[]--> core
```

Every arrow in that picture is checked. A name on the right-hand side that nothing declares
is a finding, with the element that named it and the name it wanted; so is a name declared
twice. The whole list, with what each check requires and which files it reads, is on
[The checks](reference/rules.md).

Two commands make the graph visible without changing anything:

```sh
swcraft list --project swcraft.yaml
swcraft impact --project swcraft.yaml IExample
```

`list` prints every declared element by layer. `impact` answers the question you actually
have before a rename or a deletion — *what points at this?* — transitively, so an interface
reports the ports typed by it, the connectors carrying it and the tasks that schedule the
runnables involved; asked of a requirement, it lists the elements that satisfy it and the
requirements derived from it. Both always exit 0 when they managed to read the project, so
neither is a gate; they are for looking.

## What a name has to be { #what-a-name-has-to-be }

<!-- purpose: The two rules a name has to follow, and why each one exists -->

Most of an element's name is your business. Two rules are not, and both exist because the
name is *emitted* somewhere that has its own grammar.

**Names that reach the AUTOSAR file** — components, ports, runnables, interfaces, data
types, connectors, and compositions with their boundary ports, members and delegations — must
be legal AUTOSAR identifiers: a letter first, then letters, digits
and underscores, up to 128 characters. No hyphens, no dots, no spaces, no leading digit.

The 128 is the part that surprises people, because SWCraft *lengthens* some names on the way
out: an enumeration that states `literals` gets a `_CompuMethod`, a component gets an
`_Implementation` and an `_InternalBehavior`, a
runnable's `timing` and `init` events get `TMT_` and `IT_` prefixes, an `operation` event is named
after its port and operation — `OIT_S_DiagnosticRecords_GetDiagnosticRecord` — and a `data` event
after its port and element — `DRT_R_WheelSpeed_FrontLeft` — and a `call_returned` event after its
port and operation — `ASRT_rNvStorage_WriteBlock` — a data element's initial value gets a
constant named after its interface and itself — `IVehicleSpeed_speed_Init` — and each entry of a
runnable's `reads`, `writes` and `calls` gets an access point named after the port and the member
it names: `R_pSpeed_speed`, `W_pSpeed_speed`, `Call_rDiag_readDtc`; a polled call and a
`call_returned` event get a result point named the same way, `Result_rNvStorage_WriteBlock`. A physical type gets one form
per physical field it declares on itself — `_Unit` for its `unit`, `_Compu` for its `conversion`,
`_Range` for its `range` — and no form for a field it leaves out or names by reference; a value type
that declares `value_min` or `value_max` gets a `_Range` too. A name that fits at 126
characters can be refused for the form generated from it, and the message names the generated form
so you can see which one. **A name you state is not lengthened**: where a component states its
`behavior_name`, an event its `name`, an access entry its `name` or a polled call or `call_returned`
event its `result`, that name is written instead of
the generated form, it is checked as the name you declared — on its own, with no budget shared with
another — and stating it is a repair for a generated form that is too long, as much as shortening.
The `_Implementation` form has no name to state in its place.

The eight forms built from **two** names are the ones to watch, because each is built from two names you wrote and
the two share one budget — except where an access point's or an event's name is stated in
place of its form (a result point's name is stated as `result`). An interface and each of its data elements share 122 characters between
them, so a long interface name leaves less room for its elements — and that limit applies to every
data element of a sender-receiver interface, whether or not it declares an initial value today,
because otherwise adding the first value to one interface would start failing the export on an
interface file nobody had touched. A port and each data element a runnable reads or writes through
it share **125**; a port and each operation a runnable calls through it share **122**; and a port and
each operation an `operation` event names share **123**, as do a port and each data element a `data`
event names. A port and the operation of a polled call or of a `call_returned` event share **120** for
the result point, and a port and each operation a `call_returned` event names share **122** for the event.
Those seven join names that live in two different files, the port on the component and the member on the interface. In every case the
message names both names and tells you which one to shorten.

Two `operation` events of one component can produce the same name the same way — a port `pA_B`
serving `C` and a port `pA` serving `B_C` both give `OIT_pA_B_C` — and so can two `data` events or two
`call_returned` events. Unlike an access point, that is not
refused: the generated file numbers the two events `OIT_pA_B_C_0` and `OIT_pA_B_C_1`, as it numbers
any two events whose names coincide, and the budget above is then taken from the numbered name.

There is one more thing two names can do together. Because both may contain an underscore, two
*different* entries of one runnable can produce the same access point name — a port `pA_B` reading
`C` and a port `pA` reading `B_C` both give `R_pA_B_C`. `swcraft check-model` reports that, naming
the shared name and every entry claiming it, and generation refuses it; so are two entries whose
stated names are equal. Nothing is renamed automatically: neither of the two names is SWCraft's to
change, so the repair is yours — a rename, usually of one character, or a `name` stated on one of
the entries.

`swcraft check-model` reports every offending name at once. It is worth running early: the
rule applies to the name as emitted, and finding out at ARXML generation time is finding out
late.

**Sequence names follow a different rule**, because they name the files of the scenario's
sequence diagram instead: a letter or an underscore first, then letters, digits and underscores.
A leading underscore is fine here and not in AUTOSAR, which is the whole of the difference. Two
sequences whose names differ only in capitalisation would name the same file on Windows and
macOS, so the diagrams refuse them.

**Component group directories follow no rule at all.** They are directory names, so
`swc/brake-ecu/` is accepted everywhere. Two group directories whose names
differ only in case, such as `swc/Body/` beside `swc/body/`, are not reported either, although
macOS and Windows hold them as one.

## Changing a model that already exists { #changing-a-model-that-already-exists }

<!-- purpose: Changing an existing model safely: impact first, then edit, check, diff and validate -->

The workflow that keeps the checks useful rather than annoying is short.

1. **Ask what points at it** before you touch a name — `swcraft impact`, above.
2. **Edit the files.** The editor helps while you type if you have the extension; see
   [In VS Code](vscode.md). Those hovers and squiggles are conveniences, not the gate.
3. **Run the model check.** `swcraft check-model --project swcraft.yaml` reads everything
   and writes nothing, so it is fast and safe to run constantly, and it is what a build
   pipeline runs first.
4. **See what you actually changed.** `swcraft diff --project swcraft.yaml` compares two
   revisions of the model in the model's vocabulary — elements added, removed and changed —
   rather than as YAML lines.
5. **Run the gate.** `swcraft validate --project swcraft.yaml` is the one whose answer
   counts. Read that answer rather than the directory listing: a run that gets as far as the
   AUTOSAR file and then fails the schema check leaves that file behind, and it is the refused
   one. [Working at the command line](cli.md) says which failures leave what.

If you are letting the assistant make the change, all five of these still happen — it runs
the checks itself before it will commit anything, and it refuses a change that breaks a check
that is an error; a warning it lets through, and `swcraft validate` lists it afterwards.
[The assistant](assistant.md) is that story.

## Where to go next { #where-to-go-next }

<!-- purpose: Where to read next: every key of a layer, every check and the diagrams -->

| If you want to | Read |
|---|---|
| look up every key of a layer, with its default | [The model layers](reference/model/index.md) |
| see every check and what it requires | [The checks](reference/rules.md) |
| read the generated diagrams | [Diagrams](diagrams.md) |
| run all of this from a terminal or a pipeline | [Command line](cli.md) |
