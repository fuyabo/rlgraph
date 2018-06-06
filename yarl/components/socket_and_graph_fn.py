# Copyright 2018 The YARL-Project, All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools
from collections import OrderedDict
import re

from yarl import YARLError
from yarl.spaces import Space
from yarl.utils.util import force_tuple
from yarl.utils.ops import SingleDataOp, FlattenedDataOp, DataOpRecord
from yarl.spaces.space_utils import flatten_op, get_space_from_op, split_flattened_input_ops, unflatten_op, \
    convert_ops_to_op_records


class Socket(object):
    """
    A Socket object describes a connection to other Sockets, GraphFunctions, or Spaces inside and between ModelComponents.
    One Socket either carries:

    - a single op (e.g. some tensor)
    - a tuple of ops (nesting also supported)
    - a dict of ops (nesting also supported)

    Also, each of the above possibilities can have many parallel solutions. These splits happen e.g. if two Sockets
    connect to the same target Socket. In this case, the target Socket's inputs are treated as possible alternatives
    and the Socket then implicitly produces two outputs that it further passes on to the next Sockets/GraphFunctions.

    When connected to a GraphFunction object, a Socket always represents one of the input parameters to the graph_fn
    method. Also, each returned value of a graph_fn method corresponds to one Socket.
    """
    def __init__(self, name, component, type_="in"):
        """
        Args:
            name (str): The name of this Socket (as it will show in the final call interface).
            component (Component): The Component object that this Socket belongs to.
            type_ (str): The Socket type: "in" or "out".
        """
        # The name of this Socket.
        self.name = name
        # "in" or "out"
        self.type = type_
        # The Component that this Socket belongs to.
        self.component = component

        # Which Socket(s), Space(s), GraphFunction(s) are we connected to on the incoming and outgoing sides?
        # - Records in these lists have a parallel relationship (they are all alternatives to each other).
        self.incoming_connections = list()
        self.outgoing_connections = list()

        # The inferred Space coming into this Socket.
        self.space = None

        # A Socket ([from-sock]) takes a label for a specific outgoing connection ([to-sock]) via a call to:
        # `Component.connect([from-sock], [to-sock], label="lab1")`.
        # The following rules apply now:
        # - [from-sock] will take all incoming ops (unless the incoming sock has its own filtering/labelling going on).
        # - [from-sock] will only pass to [to-sock] (AND label with "lab1") those ops that either have no label yet or
        #   that already carry the label "lab1". All other ops will not be passed to [to-sock].
        # Also, when sent through a graph_fn, the resulting ops carry a union of all input ops' labels.
        # key=[to-sock]'s component/name string; values=set of labels (str).
        self.labels = dict()

        # The set of (alternative) DataOpRecords (op plus optional label(s)).
        self.op_records = set()

    def connect_to(self, to_):
        """
        Adds an outgoing connection to this Socket, either to a Space, a GraphFunction or to another Socket.
        This means that this Socket will pass its DataOps over to `to_` during build time.

        Args:
            to_ (Union[Socket,GraphFunction]): The Socket/GraphFunction that we are connecting to.
        """
        if to_ not in self.outgoing_connections:
            self.outgoing_connections.append(to_)

    def disconnect_to(self, to_):
        """
        Equivalent to `self.connect_to`.
        """
        if to_ in self.outgoing_connections:
            self.outgoing_connections.remove(to_)

    def connect_from(self, from_, label=None):
        """
        Adds an incoming connection to this Socket, either from a Space, a GraphFunction or from another Socket.
        This means that this Socket will receive `from_`'s ops during build time.
        
        Args:
            from_ (Union[Socket,Space,GraphFunction]): The Socket/Space/GraphFunction that we are connected from.
            label (Optional[str]): A possible label to give to `from_`. This label will be passed along with
                `from_`'s ops during build time.
        """
        if from_ not in self.incoming_connections:
            # We need to add this Socket here to the list of no-input entry points (except core as we always start
            # building with the core's in-Sockets, so these are covered).
            if isinstance(from_, SingleDataOp):
                if self.component.is_core is False:
                    self.component.no_input_entry_points.append(self)
            # Socket: Add the label for ops passed to this Socket.
            elif label is not None:
                assert isinstance(from_, Socket), "ERROR: No `label` ({}) allowed if `from_` ({}) is not a Socket " \
                                                  "object!".format(label, str(from_))
                if self not in from_.labels:
                    from_.labels[self] = set()
                from_.labels[self].add(label)
            self.incoming_connections.append(from_)

    def disconnect_from(self, from_):
        """
        Equivalent to `self.connect_from`.
        """
        if from_ in self.incoming_connections:
            if isinstance(from_, SingleDataOp):
                self.component.no_input_entry_points.remove(self)
            self.incoming_connections.remove(from_)

    def update_from_input(self, incoming, op_record_registry, in_socket_registry, graph_fn_in_slot=None,
                          in_socket_op_record=None):
        """
        Updates this socket based on an incoming connection (from a Space, GraphFunction or another Socket).

        Args:
            incoming (Union[Space,GraphFunction,Socket]): The incoming item.
            op_record_registry (dict): Dict that keeps track of which op-record requires which other op-records
                to be calculated.
            in_socket_registry (dict): Dict that keeps track of which very in-Socket (name) needs which
                ops (placeholders/feeds).
            graph_fn_in_slot (Optional[int]): If incoming is a GraphFunction, which output slot does this Socket
                connect to?
            in_socket_op_record (Optional[DataOpRecord]): If incoming is a Socket, this may hold a DataOpRecord that we
                should build from. If None and incoming is of type Socket, update from all the Socket's DataOpRecords.

        Raises:
            YARLError: If there is an attempt to connect more than one Space to this Socket.
        """
        # Incoming is another Socket -> Simply update ops from this one.
        if isinstance(incoming, Socket):
            if incoming.space is not None:
                assert self.space is None or incoming.space == self.space
                self.space = incoming.space
                self.component.check_input_completeness()
                # Make sure we filter those op-records that already have at least one label and that do not
                # have the label of this connection (from `incoming`).
                socket_labels = incoming.labels.get(self, None)  # type: set
                op_records = set(in_socket_op_record or incoming.op_records)  # force a set, even if just single item
                # With filtering.
                if socket_labels is not None:
                    filtered_op_records = set()
                    for op_rec in op_records:  # type: DataOpRecord
                        # If incoming op has no labels OR it has at least 1 label out of this Socket's
                        # labels for this connection -> Allow op through to this Socket.
                        if len(op_rec.labels) == 0 or len(set.intersection(op_rec.labels, socket_labels)):
                            op_rec.labels.update(socket_labels)
                            filtered_op_records.add(op_rec)
                    op_records = filtered_op_records
                self.op_records.update(op_records)

        # Space: generate backend-ops.
        elif isinstance(incoming, Space):
            # Store this Space as our incoming Space.
            if self.space is not None:
                raise YARLError("ERROR: A Socket can only have one incoming Space!")
            self.space = incoming

            op = incoming.get_tensor_variable(name=self.name, is_input_feed=True)
            op_rec = DataOpRecord(op)
            # Add new DataOp record (no labels for incoming Spaces' ops).
            self.op_records.add(op_rec)
            # Keep track of which Spaces can go (alternatively) into this Socket.
            in_socket_registry[self.name] = {op} if self.name not in in_socket_registry \
                else (in_socket_registry[self.name] | {op})
            # Remember, that this DataOp goes into a Socket at the very beginning of the Graph (e.g. a tf.placeholder).
            op_record_registry[op_rec] = {self}

        # GraphFunction: Connect this Socket to the nth op coming out of the GraphFunction function.
        elif isinstance(incoming, GraphFunction):
            assert isinstance(graph_fn_in_slot, int) and graph_fn_in_slot >= 0, \
                "ERROR: If incoming is a GraphFunction, slot must be set and >=0!"
            # Add every nth op from the output of the completed call to graph_fn to this Socket's set of ops.
            nth_computed_ops_records = list()
            for outputs in incoming.in_out_records_map.values():
                nth_computed_ops_records.append(outputs[graph_fn_in_slot])

            # Store incoming Space.
            if len(nth_computed_ops_records) > 0:
                space_check = get_space_from_op(next(iter(nth_computed_ops_records)).op)
                self.space = space_check
                # Check whether all graph_fn-computed ops have the same Space.
                for op in nth_computed_ops_records:
                    space = get_space_from_op(op.op)
                    assert space == space_check,\
                        "ERROR: Different output ops of graph_fn '{}' have different Spaces ({} and {})!". \
                        format(incoming.name, space, space_check)
            else:
                self.space = 0

            self.op_records.update(nth_computed_ops_records)

        # Constant DataOp with a value.
        elif isinstance(incoming, SingleDataOp):
            if len(self.op_records) > 1:
                raise YARLError("ERROR: A constant-value Socket may only have one such incoming value! Socket '{}' "
                                "already has {} other incoming connections.".format(self.name,
                                                                                    len(self.incoming_connections)))
            self.space = get_space_from_op(incoming)
            self.component.check_input_completeness()
            self.op_records.add(DataOpRecord(incoming))

        # Unsupported input: Error.
        else:
            raise YARLError("ERROR: `incoming` ({}) must be of type Socket, Space, GraphFunction, or SingleDataOp!".\
                            format(incoming))

    def __str__(self):
        return "{}-Socket('{}/{}'{})".format(self.type, self.component.scope, self.name,
                                             " dev='{}'".format(self.component.device) if self.component.device else "")


class GraphFunction(object):
    """
    Class describing a segment of the graph defined by a _graph_fn-method inside a Component.
    A GraphFunction is connected to incoming Sockets (these are the input parameters to the _graph-func) and to
    outgoing Sockets (these are the return values of the _graph func).

    Implements the update_from_input method which checks whether all necessary inputs to a graph_fn
    are given and - if yes - starts producing output ops from these inputs and the graph_fn to be passed
    on to the outgoing Sockets.
    """
    def __init__(self, method, component, input_sockets, output_sockets,
                 flatten_ops=True, split_ops=True,
                 add_auto_key_as_first_param=False, unflatten_ops=True):
        """
        Args:
            method (Union[str,callable]): The method of the graph_fn (must be the name (w/o _graph prefix)
                of a method in `component` or directly a callable.
            component (Component): The Component object that this GraphFunction belongs to.
            input_sockets (List[Socket]): The required input Sockets to be passed as parameters into the
                graph_fn. In the order of graph_fn's parameters.
            output_sockets (List[socket]): The Sockets associated with the return values coming from the graph_fn.
                In the order of the returned values.
            flatten_ops (Union[bool,Set[str]]): Whether to flatten all or some DataOps by creating
                a FlattenedDataOp (with automatic key names).
                Can also be a set of in-Socket names to flatten explicitly (True for all).
                (default: True).
            split_ops (Union[bool,Set[str]]): Whether to split all or some of the already flattened DataOps
                and send the SingleDataOps one by one through the graph_fn.
                Example: in-Sockets=A=Dict (container), B=int (primitive)
                    The graph_fn should then expect for each primitive Space in A:
                        _graph_fn(primitive-in-A (Space), B (int))
                        NOTE that B will be the same in all calls for all primitive-in-A's.
                (default: True).
            add_auto_key_as_first_param (bool): If `split_ops` is not False, whether to send the
                automatically generated flat key as the very first parameter into each call of the graph_fn.
                Example: in-Sockets=A=float (primitive), B=Tuple (container)
                    The graph_fn should then expect for each primitive Space in B:
                        _graph_fn(key, A (float), primitive-in-B (Space))
                        NOTE that A will be the same in all calls for all primitive-in-B's.
                        The key can now be used to index into variables equally structured as B.
                Has no effect if `split_ops` is False.
                (default: False).
            unflatten_ops (bool): Whether to re-establish a nested structure of DataOps
                for graph_fn-returned FlattenedDataOps.
                (default: True)

        Raises:
            YARLError: If a graph_fn with the given name cannot be found in the component.
        """

        # The component object that the method belongs to.
        self.component = component

        self.flatten_ops = flatten_ops
        self.split_ops = split_ops
        self.add_auto_key_as_first_param = add_auto_key_as_first_param
        self.unflatten_ops = unflatten_ops

        if isinstance(method, str):
            self.name = method
            self.method = getattr(self.component, "_graph_fn_" + method, None)
            if not self.method:
                raise YARLError("ERROR: No `_graph_fn_...` method with name '{}' found!".format(method))
        else:
            self.method = method
            self.name = re.sub(r'^_graph_fn_', "", method.__name__)

        # Dict-records for input-sockets (by name) to keep information on their position and "op-completeness".
        self.input_sockets = OrderedDict()
        for i, in_sock in enumerate(input_sockets):
            self.input_sockets[in_sock.name] = dict(socket=in_sock, pos=i)  # OBSOLETE:, op_records=set())
        # Just a list of Socket objects.
        self.output_sockets = output_sockets

        # Whether we have all necessary input-sockets for passing at least one input-op combination through
        # our computation method. As long as this is False, we return prematurely and wait for more ops to come in
        # (through other Sockets).
        self.input_complete = False
        # Registry for which incoming Sockets' op-records we have already passed through the graph_fn to generate
        # which output op-records.
        # key=tuple of input-op-records (len==number of input params).
        # value=list of generated output op-records (len==number of return values).
        self.in_out_records_map = dict()

    #def to_graph(self, method):
    #    """
    #    Converts function containing Python control flow to graph.
    #
    #    Args:
    #        method (callable): Function object containing computations and potentially control flow.
    #
    #    Returns:
    #        GraphFunction graph object.
    #    """
    #    return method  # not mandatory

    def update_from_input(self, input_socket, op_record_registry):
        """
        Updates our "waiting" inputs with the incoming socket and checks whether this computation is "input-complete".
        If yes, do all possible combinatorial pass-throughs through the computation function to generate output ops
        and assign these ops to our respective output sockets (first socket gets first output op, etc.. depending
        on how many return values the computation function has).

        Args:
            input_socket (Optional[Socket]): The incoming Socket (OBSOLETE: by design, must be type "in").
                None, if this GraphFunction has no in-Sockets anyway.
            op_record_registry (dict): Dict that keeps track of which op-record requires which other op-records
                to be calculated.
        """
        # This GraphFunction has no in-Sockets.
        if input_socket is None:
            self.input_complete = True
            # Call the method w/o any parameters.
            ops = force_tuple(self.method())
            if ops == ():
                raise YARLError("ERROR: {}'s computation method '{}' does not return an op!".
                                format(self.component.name, self.method.__name__))
            op_records = convert_ops_to_op_records(ops)
            # Use empty tuple as input-op-records combination.
            self.in_out_records_map[()] = list(op_records)
            # Tag all out-ops as not requiring any input.
            for op_rec in op_records:
                op_record_registry[op_rec] = set()

            return

        # Check for input-completeness of this graph_fn.
        self.check_input_completeness()

        # We are input-complete: Get all possible combinations of input ops and pass all these
        # combinations through the function.
        if self.input_complete:
            self.generate_data_ops(op_record_registry)

    def generate_data_ops(self, op_record_registry):
        """
        Generates a list of all possible input DataOp combinations to be passed through the graph_fn.

        Args:
            op_record_registry (dict): Dict that keeps track of which op-record requires which other op-records
                to be calculated.
        """
        in_op_records = [in_sock_rec["socket"].op_records for in_sock_rec in self.input_sockets.values()]
        in_op_records_combinations = list(itertools.product(*in_op_records))
        for in_op_record_combination in in_op_records_combinations:
            # key = tuple(input_combination)
            # Make sure we call the computation method only once per input-op combination.
            # if in_op_combination_wo_constant_values not in self.in_out_records_map:
            if in_op_record_combination not in self.in_out_records_map:
                # Replace constant-value Sockets with their SingleDataOp's constant numpy values
                # and the DataOps with their actual ops (`op` property of DataOp).
                actual_call_params = [
                    op_rec.op.constant_value if isinstance(op_rec.op, SingleDataOp) and
                                                op_rec.op.constant_value is not None else op_rec.op for op_rec in
                    in_op_record_combination
                ]

                # Build the ops from this input-combination.
                # Flatten input items.
                if self.flatten_ops is not False:
                    flattened_ops = self.flatten_input_ops(*actual_call_params)
                    # Split into SingleDataOps?
                    if self.split_ops:
                        call_params = split_flattened_input_ops(self.add_auto_key_as_first_param, *flattened_ops)
                        # There is some splitting to do. Call graph_fn many times (one for each split).
                        if isinstance(call_params, FlattenedDataOp):
                            ops = FlattenedDataOp()
                            for key, params in call_params.items():
                                ops[key] = self.method(*params)
                        # No splitting to do. Pass everything once and as-is.
                        else:
                            ops = self.method(*call_params)
                    else:
                        ops = self.method(*flattened_ops)
                # Just pass in everything as-is.
                else:
                    ops = self.method(*actual_call_params)

                # Need to un-flatten return values?
                if self.unflatten_ops:
                    ops = self.unflatten_output_ops(*force_tuple(ops))

                # Make sure everything coming from a computation is always a tuple (for out-Socket indexing).
                ops = force_tuple(ops)

                # ops are now the raw graph_fn output: Need to convert it back to records.
                new_label_set = set()
                for rec in in_op_record_combination:  # type: DataOpRecord
                    new_label_set.update(rec.labels)
                op_records = convert_ops_to_op_records(ops, labels=new_label_set)

                self.in_out_records_map[in_op_record_combination] = op_records
                # Keep track of which ops require which other ops.
                for op_rec in op_records:
                    op_record_registry[op_rec] = set(in_op_record_combination)
                    # Make sure all op_records do not contain SingleDataOps with constant_values. Any
                    # in-Socket-connected constant values need to be converted to actual ops during a graph_fn call.
                    assert not isinstance(op_rec.op, SingleDataOp), \
                        "ERROR: graph_fn '{}' returned a SingleDataOp with constant_value set to '{}'! " \
                        "This is not allowed. All graph_fns must return actual (non-constant) ops.". \
                        format(self.name, op_rec.op.constant_value)
            # Error.
            else:
                pass
                # raise YARLError("ERROR: `in_op_record_combination`='{}' already in self.in_out_records_map!".
                #            format(in_op_record_combination))

    def check_input_completeness(self):
        """
        Checks whether this GraphFunction is "input-complete" and stores the result in self.input_complete.
        Input-completeness is reached (only once and then it stays that way) if all in-Sockets to this computation
        have at least one op defined in their Socket.op_records set.
        """
        if not self.input_complete:
            # Check, whether we are input-complete now (whether all in-Sockets have at least one op defined).
            self.input_complete = True
            for in_sock_rec in self.input_sockets.values():
                if len(in_sock_rec["socket"].op_records) == 0:
                    self.input_complete = False
                    return

    def flatten_input_ops(self, *ops):
        """
        Flattens all DataOps in ops into FlattenedDataOp with auto-key generation.
        Ops whose Sockets are not in self.flatten_ops (if its a set)
        will be ignored.

        Args:
            *ops (DataOp): The items to flatten.

        Returns:
            tuple: All *ops as FlattenedDataOp.
        """
        # The returned sequence of output ops.
        ret = []
        in_socket_names = self.input_sockets.keys()
        for i, op in enumerate(ops):
            # self.flatten_ops cannot be False here.
            if self.flatten_ops is True or (isinstance(self.flatten_ops, set) and
                                            in_socket_names[i] in self.flatten_ops):
                ret.append(flatten_op(op))
            else:
                ret.append(op)

        # Always return a tuple for indexing into the return values.
        return tuple(ret)

    @staticmethod
    def unflatten_output_ops(*ops):
        """
        Re-creates the originally nested input structure (as DataOpDict/DataOpTuple) of the given output ops.
        Process all FlattenedDataOp with auto-generated keys, and leave the others untouched.

        Args:
            *ops (DataOp): The ops that need to be re-nested (only process the FlattenedDataOp
                amongst these and ignore all others).

        Returns:
            Tuple[DataOp]: A tuple containing the ops as they came in, except that all FlattenedDataOp
                have been un-flattened (re-nested) into their original ContainerDataOp structures.
        """
        # The returned sequence of output ops.
        ret = []

        for i, op in enumerate(ops):
            # A FlattenedDataOp: Try to re-nest it and then compare it to input_template_op's structure.
            if isinstance(op, dict):  # allow any dict to be un-flattened
                ret.append(unflatten_op(op))
            # All others are left as-is.
            else:
                ret.append(op)

        # Always return a tuple for indexing into the return values.
        return tuple(ret)

    def __str__(self):
        return "{}('{}' in=[{}] out=[{}])". \
            format(type(self).__name__, self.name, str(self.input_sockets), str(self.output_sockets))
