# This code is part of Qiskit.
#
# (C) Copyright IBM 2017, 2020.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""
=========================================================================================
Unitary Synthesis Plugin (in :mod:`qiskit.transpiler.passes.synthesis.unitary_synthesis`)
=========================================================================================

.. autosummary::
   :toctree: ../stubs/

   DefaultUnitarySynthesis
"""

from __future__ import annotations
from math import pi, inf, isclose
from itertools import product
from functools import partial
import numpy as np

from qiskit.circuit import Gate, Parameter
from qiskit.circuit.library.standard_gates import (
    iSwapGate,
    CXGate,
    CZGate,
    RXXGate,
    RZXGate,
    RZZGate,
    RYYGate,
    ECRGate,
    RXGate,
    SXGate,
    XGate,
    RZGate,
    UGate,
    PhaseGate,
    U1Gate,
    U2Gate,
    U3Gate,
    RYGate,
    RGate,
    CRXGate,
    CRYGate,
    CRZGate,
    CPhaseGate,
)
from qiskit.converters import circuit_to_dag
from qiskit.dagcircuit.dagcircuit import DAGCircuit
from qiskit.dagcircuit.dagnode import DAGOpNode
from qiskit.exceptions import QiskitError
from qiskit.quantum_info import Operator
from qiskit.synthesis.two_qubit.xx_decompose import XXDecomposer, XXEmbodiments
from qiskit.synthesis.two_qubit.two_qubit_decompose import (
    TwoQubitBasisDecomposer,
    TwoQubitWeylDecomposition,
    TwoQubitControlledUDecomposer,
)
from qiskit.transpiler.exceptions import TranspilerError
from qiskit.transpiler.passes.optimization.optimize_1q_decomposition import (
    Optimize1qGatesDecomposition,
    _possible_decomposers,
)
from qiskit.transpiler.passes.synthesis import plugin


GATE_NAME_MAP = {
    "cx": CXGate._standard_gate,
    "rx": RXGate._standard_gate,
    "sx": SXGate._standard_gate,
    "x": XGate._standard_gate,
    "rz": RZGate._standard_gate,
    "u": UGate._standard_gate,
    "p": PhaseGate._standard_gate,
    "u1": U1Gate._standard_gate,
    "u2": U2Gate._standard_gate,
    "u3": U3Gate._standard_gate,
    "ry": RYGate._standard_gate,
    "r": RGate._standard_gate,
    "rzz": RZZGate._standard_gate,
    "ryy": RYYGate._standard_gate,
    "rxx": RXXGate._standard_gate,
    "rzx": RXXGate._standard_gate,
    "cp": CPhaseGate._standard_gate,
    "crx": RXXGate._standard_gate,
    "cry": RXXGate._standard_gate,
    "crz": RXXGate._standard_gate,
}

KAK_GATE_PARAM_NAMES = {
    "rxx": RXXGate,
    "rzz": RZZGate,
    "ryy": RYYGate,
    "rzx": RZXGate,
    "cphase": CPhaseGate,
    "crx": CRXGate,
    "cry": CRYGate,
    "crz": CRZGate,
}

KAK_GATE_NAMES = {
    "cx": CXGate(),
    "cz": CZGate(),
    "iswap": iSwapGate(),
    "ecr": ECRGate(),
}


def _choose_kak_gate(basis_gates):
    """Choose the first available 2q gate to use in the KAK decomposition."""
    kak_gate = None
    kak_gates = sorted(set(basis_gates or []).intersection(KAK_GATE_NAMES.keys()))
    kak_gates_params = sorted(set(basis_gates or []).intersection(KAK_GATE_PARAM_NAMES.keys()))

    if kak_gates_params:
        kak_gate = KAK_GATE_PARAM_NAMES[kak_gates_params[0]]

    elif kak_gates:
        kak_gate = KAK_GATE_NAMES[kak_gates[0]]

    return kak_gate


def _choose_euler_basis(basis_gates):
    """Choose the first available 1q basis to use in the Euler decomposition."""
    basis_set = set(basis_gates or [])
    decomposers = _possible_decomposers(basis_set)
    if decomposers:
        return decomposers[0]
    return "U"


def _find_matching_euler_bases(target, qubit):
    """Find matching available 1q basis to use in the Euler decomposition."""
    basis_set = target.operation_names_for_qargs((qubit,))
    return _possible_decomposers(basis_set)


def _decomposer_2q_from_basis_gates(basis_gates, pulse_optimize=None, approximation_degree=None):
    decomposer2q = None
    kak_gate = _choose_kak_gate(basis_gates)
    euler_basis = _choose_euler_basis(basis_gates)
    basis_fidelity = approximation_degree or 1.0

    if kak_gate in KAK_GATE_PARAM_NAMES.values():
        decomposer2q = TwoQubitControlledUDecomposer(kak_gate, euler_basis)
    elif kak_gate is not None:
        decomposer2q = TwoQubitBasisDecomposer(
            kak_gate,
            basis_fidelity=basis_fidelity,
            euler_basis=euler_basis,
            pulse_optimize=pulse_optimize,
        )
    return decomposer2q


def _error(circuit, target=None, qubits=None):
    """
    Calculate a rough error for a `circuit` that runs on specific
    `qubits` of `target`.

    Use basis errors from target if available, otherwise use length
    of circuit as a weak proxy for error.
    """
    if target is None:
        if isinstance(circuit, DAGCircuit):
            return len(circuit.op_nodes())
        else:
            return len(circuit)
    gate_fidelities = []
    gate_durations = []

    def score_instruction(inst, inst_qubits):
        try:
            keys = target.operation_names_for_qargs(inst_qubits)
            for key in keys:
                target_op = target.operation_from_name(key)
                if isinstance(circuit, DAGCircuit):
                    op = inst.op
                else:
                    op = inst.operation
                if isinstance(target_op, op.base_class) and (
                    target_op.is_parameterized()
                    or all(
                        isclose(float(p1), float(p2)) for p1, p2 in zip(target_op.params, op.params)
                    )
                ):
                    inst_props = target[key].get(inst_qubits, None)
                    if inst_props is not None:
                        error = getattr(inst_props, "error", 0.0) or 0.0
                        duration = getattr(inst_props, "duration", 0.0) or 0.0
                        gate_fidelities.append(1 - error)
                        gate_durations.append(duration)
                    else:
                        gate_fidelities.append(1.0)
                        gate_durations.append(0.0)

                    break
            else:
                raise KeyError
        except KeyError as error:
            if isinstance(circuit, DAGCircuit):
                op = inst.op
            else:
                op = inst.operation
            raise TranspilerError(
                f"Encountered a bad synthesis. " f"Target has no {op} on qubits {qubits}."
            ) from error

    if isinstance(circuit, DAGCircuit):
        for inst in circuit.topological_op_nodes():
            inst_qubits = tuple(qubits[circuit.find_bit(q).index] for q in inst.qargs)
            score_instruction(inst, inst_qubits)
    else:
        for inst in circuit:
            inst_qubits = tuple(qubits[circuit.find_bit(q).index] for q in inst.qubits)
            score_instruction(inst, inst_qubits)
    # TODO:return np.sum(gate_durations)
    return 1 - np.prod(gate_fidelities)


def _preferred_direction(
    decomposer2q, qubits, natural_direction, coupling_map=None, gate_lengths=None, gate_errors=None
):
    """
    `decomposer2q` decomposes an SU(4) over `qubits`. A user sets `natural_direction`
    to indicate whether they prefer synthesis in a hardware-native direction.
    If yes, we return the `preferred_direction` here. If no hardware direction is
    preferred, we raise an error (unless natural_direction is None).
    We infer this from `coupling_map`, `gate_lengths`, `gate_errors`.

    Returns [0, 1] if qubits are correct in the hardware-native direction.
    Returns [1, 0] if qubits must be flipped to match hardware-native direction.
    """
    qubits_tuple = tuple(qubits)
    reverse_tuple = qubits_tuple[::-1]

    preferred_direction = None
    if natural_direction in {None, True}:
        # find native gate directions from a (non-bidirectional) coupling map
        if coupling_map is not None:
            neighbors0 = coupling_map.neighbors(qubits[0])
            zero_one = qubits[1] in neighbors0
            neighbors1 = coupling_map.neighbors(qubits[1])
            one_zero = qubits[0] in neighbors1
            if zero_one and not one_zero:
                preferred_direction = [0, 1]
            if one_zero and not zero_one:
                preferred_direction = [1, 0]
        # otherwise infer natural directions from gate durations or gate errors
        if preferred_direction is None and (gate_lengths or gate_errors):
            cost_0_1 = inf
            cost_1_0 = inf
            try:
                cost_0_1 = next(
                    duration
                    for gate, duration in gate_lengths.get(qubits_tuple, [])
                    if gate == decomposer2q.gate
                )
            except StopIteration:
                pass
            try:
                cost_1_0 = next(
                    duration
                    for gate, duration in gate_lengths.get(reverse_tuple, [])
                    if gate == decomposer2q.gate
                )
            except StopIteration:
                pass
            if not (cost_0_1 < inf or cost_1_0 < inf):
                try:
                    cost_0_1 = next(
                        error
                        for gate, error in gate_errors.get(qubits_tuple, [])
                        if gate == decomposer2q.gate
                    )
                except StopIteration:
                    pass
                try:
                    cost_1_0 = next(
                        error
                        for gate, error in gate_errors.get(reverse_tuple, [])
                        if gate == decomposer2q.gate
                    )
                except StopIteration:
                    pass
            if cost_0_1 < cost_1_0:
                preferred_direction = [0, 1]
            elif cost_1_0 < cost_0_1:
                preferred_direction = [1, 0]
    if natural_direction is True and preferred_direction is None:
        raise TranspilerError(
            f"No preferred direction of gate on qubits {qubits} "
            "could be determined from coupling map or "
            "gate lengths / gate errors."
        )
    return preferred_direction


class DefaultUnitarySynthesis(plugin.UnitarySynthesisPlugin):
    """The default unitary synthesis plugin."""

    @property
    def supports_basis_gates(self):
        return True

    @property
    def supports_coupling_map(self):
        return True

    @property
    def supports_natural_direction(self):
        return True

    @property
    def supports_pulse_optimize(self):
        return True

    @property
    def supports_gate_lengths(self):
        return False

    @property
    def supports_gate_errors(self):
        return False

    @property
    def supports_gate_lengths_by_qubit(self):
        return True

    @property
    def supports_gate_errors_by_qubit(self):
        return True

    @property
    def max_qubits(self):
        return None

    @property
    def min_qubits(self):
        return None

    @property
    def supported_bases(self):
        return None

    @property
    def supports_target(self):
        return True

    def __init__(self):
        super().__init__()
        self._decomposer_cache = {}

    def _decomposer_2q_from_target(self, target, qubits, approximation_degree):
        # we just need 2-qubit decomposers, in any direction.
        # we'll fix the synthesis direction later.
        qubits_tuple = tuple(sorted(qubits))
        reverse_tuple = qubits_tuple[::-1]
        if qubits_tuple in self._decomposer_cache:
            return self._decomposer_cache[qubits_tuple]

        # available instructions on this qubit pair, and their associated property.
        available_2q_basis = {}
        available_2q_props = {}

        # 2q gates sent to 2q decomposers must not have any symbolic parameters.  The
        # gates must be convertable to a numeric matrix. If a basis gate supports an arbitrary
        # angle, we have to choose one angle (or more.)
        def _replace_parameterized_gate(op):
            if isinstance(op, RXXGate) and isinstance(op.params[0], Parameter):
                op = RXXGate(pi / 2)
            elif isinstance(op, RZXGate) and isinstance(op.params[0], Parameter):
                op = RZXGate(pi / 4)
            elif isinstance(op, RZZGate) and isinstance(op.params[0], Parameter):
                op = RZZGate(pi / 2)
            return op

        try:
            keys = target.operation_names_for_qargs(qubits_tuple)
            for key in keys:
                op = target.operation_from_name(key)
                if not isinstance(op, Gate):
                    continue
                available_2q_basis[key] = _replace_parameterized_gate(op)
                available_2q_props[key] = target[key][qubits_tuple]
        except KeyError:
            pass
        try:
            keys = target.operation_names_for_qargs(reverse_tuple)
            for key in keys:
                if key not in available_2q_basis:
                    op = target.operation_from_name(key)
                    if not isinstance(op, Gate):
                        continue
                    available_2q_basis[key] = _replace_parameterized_gate(op)
                    available_2q_props[key] = target[key][reverse_tuple]
        except KeyError:
            pass
        if not available_2q_basis:
            raise TranspilerError(
                f"Target has no gates available on qubits {qubits} to synthesize over."
            )
        # available decomposition basis on each of the qubits of the pair
        # NOTE: assumes both qubits have the same single-qubit gates
        available_1q_basis = _find_matching_euler_bases(target, qubits_tuple[0])

        # find all decomposers
        # TODO: reduce number of decomposers here somehow
        decomposers = []

        def is_supercontrolled(gate):
            try:
                operator = Operator(gate)
            except QiskitError:
                return False
            kak = TwoQubitWeylDecomposition(operator.data)
            return isclose(kak.a, pi / 4) and isclose(kak.c, 0.0)

        def is_controlled(gate):
            try:
                operator = Operator(gate)
            except QiskitError:
                return False
            kak = TwoQubitWeylDecomposition(operator.data)
            return isclose(kak.b, 0.0) and isclose(kak.c, 0.0)

        # possible supercontrolled decomposers (i.e. TwoQubitBasisDecomposer)
        supercontrolled_basis = {
            k: v for k, v in available_2q_basis.items() if is_supercontrolled(v)
        }
        for basis_1q, basis_2q in product(available_1q_basis, supercontrolled_basis.keys()):
            props = available_2q_props.get(basis_2q)
            if props is None:
                basis_2q_fidelity = 1.0
            else:
                error = getattr(props, "error", 0.0)
                if error is None:
                    error = 0.0
                basis_2q_fidelity = 1 - error
            if approximation_degree is not None:
                basis_2q_fidelity *= approximation_degree
            decomposer = TwoQubitBasisDecomposer(
                supercontrolled_basis[basis_2q],
                euler_basis=basis_1q,
                basis_fidelity=basis_2q_fidelity,
            )
            decomposers.append(decomposer)

        # If our 2q basis gates are a subset of cx, ecr, or cz then we know TwoQubitBasisDecomposer
        # is an ideal decomposition and there is no need to bother calculating the XX embodiments
        # or try the XX decomposer
        if {"cx", "cz", "ecr"}.issuperset(available_2q_basis):
            self._decomposer_cache[qubits_tuple] = decomposers
            return decomposers

        # possible controlled decomposers (i.e. XXDecomposer)
        controlled_basis = {k: v for k, v in available_2q_basis.items() if is_controlled(v)}
        basis_2q_fidelity = {}
        embodiments = {}
        pi2_basis = None
        for k, v in controlled_basis.items():
            strength = 2 * TwoQubitWeylDecomposition(Operator(v).data).a  # pi/2: fully entangling
            # each strength has its own fidelity
            props = available_2q_props.get(k)
            if props is None:
                basis_2q_fidelity[strength] = 1.0
            else:
                error = getattr(props, "error", 0.0)
                if error is None:
                    error = 0.0
                basis_2q_fidelity[strength] = 1 - error
            # rewrite XX of the same strength in terms of it
            embodiment = XXEmbodiments[v.base_class]
            if len(embodiment.parameters) == 1:
                embodiments[strength] = embodiment.assign_parameters([strength])
            else:
                embodiments[strength] = embodiment
            # basis equivalent to CX are well optimized so use for the pi/2 angle if available
            if isclose(strength, pi / 2) and k in supercontrolled_basis:
                pi2_basis = v
        # if we are using the approximation_degree knob, use it to scale already-given fidelities
        if approximation_degree is not None:
            basis_2q_fidelity = {k: v * approximation_degree for k, v in basis_2q_fidelity.items()}
        if basis_2q_fidelity:
            for basis_1q in available_1q_basis:
                if isinstance(pi2_basis, CXGate) and basis_1q == "ZSX":
                    # If we're going to use the pulse optimal decomposition
                    # in TwoQubitBasisDecomposer we need to compute the basis
                    # fidelity to use for the decomposition. Either use the
                    # cx error rate if approximation degree is None, or
                    # the approximation degree value if it's a float
                    if approximation_degree is None:
                        props = target["cx"].get(qubits_tuple)
                        if props is not None:
                            fidelity = 1.0 - getattr(props, "error", 0.0)
                        else:
                            fidelity = 1.0
                    else:
                        fidelity = approximation_degree
                    pi2_decomposer = TwoQubitBasisDecomposer(
                        pi2_basis,
                        euler_basis=basis_1q,
                        basis_fidelity=fidelity,
                        pulse_optimize=True,
                    )
                    embodiments.update({pi / 2: XXEmbodiments[pi2_decomposer.gate.base_class]})
                else:
                    pi2_decomposer = None
                decomposer = XXDecomposer(
                    basis_fidelity=basis_2q_fidelity,
                    euler_basis=basis_1q,
                    embodiments=embodiments,
                    backup_optimizer=pi2_decomposer,
                )
                decomposers.append(decomposer)

        self._decomposer_cache[qubits_tuple] = decomposers
        return decomposers

    def run(self, unitary, **options):
        # Approximation degree is set directly as an attribute on the
        # instance by the UnitarySynthesis pass here as it's not part of
        # plugin interface. However if for some reason it's not set assume
        # it's 1.
        approximation_degree = getattr(self, "_approximation_degree", 1.0)
        basis_gates = options["basis_gates"]
        coupling_map = options["coupling_map"][0]
        natural_direction = options["natural_direction"]
        pulse_optimize = options["pulse_optimize"]
        gate_lengths = options["gate_lengths_by_qubit"]
        gate_errors = options["gate_errors_by_qubit"]
        qubits = options["coupling_map"][1]
        target = options["target"]

        if unitary.shape == (2, 2):
            _decomposer1q = Optimize1qGatesDecomposition(basis_gates, target)
            sequence = _decomposer1q._resynthesize_run(unitary, qubits[0])
            if sequence is None:
                return None
            return _decomposer1q._gate_sequence_to_dag(sequence)
        elif unitary.shape == (4, 4):
            # select synthesizers that can lower to the target
            if target is not None:
                decomposers2q = self._decomposer_2q_from_target(
                    target, qubits, approximation_degree
                )
            else:
                decomposer2q = _decomposer_2q_from_basis_gates(
                    basis_gates, pulse_optimize, approximation_degree
                )
                decomposers2q = [decomposer2q] if decomposer2q is not None else []
            # choose the cheapest output among synthesized circuits
            synth_circuits = []
            # If we have a single TwoQubitBasisDecomposer skip dag creation as we don't need to
            # store and can instead manually create the synthesized gates directly in the output dag
            if len(decomposers2q) == 1 and isinstance(decomposers2q[0], TwoQubitBasisDecomposer):
                preferred_direction = _preferred_direction(
                    decomposers2q[0],
                    qubits,
                    natural_direction,
                    coupling_map,
                    gate_lengths,
                    gate_errors,
                )
                return self._synth_su4_no_dag(
                    unitary, decomposers2q[0], preferred_direction, approximation_degree
                )
            for decomposer2q in decomposers2q:
                preferred_direction = _preferred_direction(
                    decomposer2q, qubits, natural_direction, coupling_map, gate_lengths, gate_errors
                )
                synth_circuit = self._synth_su4(
                    unitary, decomposer2q, preferred_direction, approximation_degree
                )
                synth_circuits.append(synth_circuit)
            synth_circuit = min(
                synth_circuits,
                key=partial(_error, target=target, qubits=tuple(qubits)),
                default=None,
            )
        else:
            from qiskit.synthesis.unitary.qsd import (  # pylint: disable=cyclic-import
                qs_decomposition,
            )

            # only decompose if needed. TODO: handle basis better
            synth_circuit = qs_decomposition(unitary) if (basis_gates or target) else None
        if synth_circuit is None:
            return None
        if isinstance(synth_circuit, DAGCircuit):
            return synth_circuit
        return circuit_to_dag(synth_circuit)

    def _synth_su4_no_dag(self, unitary, decomposer2q, preferred_direction, approximation_degree):
        approximate = not approximation_degree == 1.0
        synth_circ = decomposer2q._inner_decomposer.to_circuit(unitary, approximate=approximate)
        if not preferred_direction:
            return (synth_circ, synth_circ.global_phase, decomposer2q.gate)

        synth_direction = None
        # if the gates in synthesis are in the opposite direction of the preferred direction
        # resynthesize a new operator which is the original conjugated by swaps.
        # this new operator is doubly mirrored from the original and is locally equivalent.
        for gate, _params, qubits in synth_circ:
            if gate is None or gate == CXGate._standard_gate:
                synth_direction = qubits
        if synth_direction is not None and synth_direction != preferred_direction:
            # TODO: Avoid using a dag to correct the synthesis direction
            return self._reversed_synth_su4(unitary, decomposer2q, approximation_degree)
        return (synth_circ, synth_circ.global_phase, decomposer2q.gate)

    def _synth_su4(self, su4_mat, decomposer2q, preferred_direction, approximation_degree):
        approximate = not approximation_degree == 1.0
        synth_circ = decomposer2q(su4_mat, approximate=approximate, use_dag=True)
        if not preferred_direction:
            return synth_circ
        synth_direction = None
        # if the gates in synthesis are in the opposite direction of the preferred direction
        # resynthesize a new operator which is the original conjugated by swaps.
        # this new operator is doubly mirrored from the original and is locally equivalent.
        for inst in synth_circ.topological_op_nodes():
            if inst.op.num_qubits == 2:
                synth_direction = [synth_circ.find_bit(q).index for q in inst.qargs]
        if synth_direction is not None and synth_direction != preferred_direction:
            return self._reversed_synth_su4(su4_mat, decomposer2q, approximation_degree)
        return synth_circ

    def _reversed_synth_su4(self, su4_mat, decomposer2q, approximation_degree):
        approximate = not approximation_degree == 1.0
        su4_mat_mm = su4_mat.copy()
        su4_mat_mm[[1, 2]] = su4_mat_mm[[2, 1]]
        su4_mat_mm[:, [1, 2]] = su4_mat_mm[:, [2, 1]]
        synth_circ = decomposer2q(su4_mat_mm, approximate=approximate, use_dag=True)
        out_dag = DAGCircuit()
        out_dag.global_phase = synth_circ.global_phase
        out_dag.add_qubits(list(reversed(synth_circ.qubits)))
        flip_bits = out_dag.qubits[::-1]
        for node in synth_circ.topological_op_nodes():
            qubits = tuple(flip_bits[synth_circ.find_bit(x).index] for x in node.qargs)
            node = DAGOpNode.from_instruction(
                node._to_circuit_instruction().replace(qubits=qubits, params=node.params)
            )
            out_dag._apply_op_node_back(node)
        return out_dag
