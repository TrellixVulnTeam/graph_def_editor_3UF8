# Copyright 2018 IBM. All Rights Reserved.
# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
"""Class to transform an subgraph into another.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial
from six import iteritems
from six import iterkeys
from six import string_types
from six import StringIO
import tensorflow as tf
import sys
if sys.version >= '3':
  from typing import Iterable

from graph_def_editor import select, subgraph, util
from graph_def_editor.node import Node
from graph_def_editor.graph import Graph
from graph_def_editor.tensor import Tensor
from graph_def_editor.variable import Variable


__all__ = [
    "replace_t_with_placeholder_handler",
    "keep_t_if_possible_handler",
    "assign_renamed_collections_handler",
    "transform_op_if_inside_handler",
    "copy_op_handler",
    "Transformer",
    "TransformerInfo",
    "copy",
    "copy_with_input_replacements",
    "graph_replace",
]


# Magical attribute name that TensorFlow uses to store colocation groups.
# See colocation_groups property below for more information.
_COLOCATION_ATTR_NAME = "_class"

# Magical prefix that TensorFlow appends to node names to create colocation
# group names.
_COLOCATION_PREFIX = "loc:@"

def replace_t_with_placeholder_handler(info, t):
  """Transform a tensor into a placeholder tensor.

  This handler is typically used to transform a subgraph input tensor into a
  placeholder.

  Args:
    info: Transform._TmpInfo instance.
    t: tensor whose input must be transformed into a place holder.
  Returns:
    The tensor generated by the newly created place holder.
  """
  return util.make_placeholder_from_tensor(info.graph_, t, scope=info.scope_)


def keep_t_if_possible_handler(info, t):
  """Transform a tensor into itself (identity) if possible.

  This handler transform a tensor into itself if the source and destination
  graph are the same. Otherwise it will create a placeholder.
  This handler is typically used to transform a hidden input tensors.

  Args:
    info: Transform._TmpInfo instance.
    t: tensor whose input must be transformed into a place holder.
  Returns:
    The tensor generated by the newly created place holder.
  """
  if info.graph is info.graph_:
    return t
  else:
    return replace_t_with_placeholder_handler(info, t)


def assign_renamed_collections_handler(info, elem, elem_):
  # type: (_TmpInfo, Any, Any) -> None
  """Add the transformed elem to the (renamed) collections of elem.

  A collection is renamed only if is not a known key, as described in
  `tf.GraphKeys`.

  Args:
    info: Transform._TmpInfo instance.
    elem: the original element (`tf.Tensor` or `tf.Operation`)
    elem_: the transformed element
  """
  known_collection_names = util.get_predefined_collection_names()
  for name, collection in iteritems(info.collections):
    if elem not in collection:
      continue

    if name in known_collection_names:
      transformed_name = name
    else:
      transformed_name = info.new_name(name)

    target_graph = info.graph_ # type: Graph
    if isinstance(elem_, (Node, Tensor, Variable)):
      elem_.add_to_collection(transformed_name)
    else:
      raise NotImplementedError("Don't know how to add name '{}' to target "
                                "graph as a collection item (target collection "
                                "name {})".format(elem_, transformed_name))


def transform_op_if_inside_handler(info, op, keep_if_possible=True):
  """Transform an optional op only if it is inside the subgraph.

  This handler is typically use to handle original op: it is fine to keep them
  if they are inside the subgraph, otherwise they are just ignored.

  Args:
    info: Transform._TmpInfo instance.
    op: the optional op to transform (or ignore).
    keep_if_possible: re-attach to the original op if possible, that is,
      if the source graph and the destination graph are the same.
  Returns:
    The transformed op or None.
  """
  if op in info.sgv.ops:
    return info.transformed_ops[op]
  else:
    if keep_if_possible and info.graph is info.graph_:
      return op
    else:
      return None


def copy_op_handler(info,
                    op, # type: node.Node
                    new_inputs, # type: Iterable[Tensor]
                    copy_shape_and_dtype=False, # type: bool
                    nodedef_fn=None
                    ):
  """Copy a node into the target graph.

  Args:
    info: Transform._TmpInfo instance.
    op: the node to be copied.
    new_inputs: The new inputs for this op. Must be tensors within the target
      graph.
    copy_shape_and_dtype: also copy the shape and dtype of the tensor. If
      `False`, shape and dtype will be inferred.
    nodedef_fn: If provided, a function that will be run on the NodeDef
      and should return a mutated NodeDef before a new Operation is created.
      This is useful as certain features cannot be set on the Operation and
      must be modified in NodeDef.

  Returns:
    A `(op, op_outputs)` tuple containing the transformed op and its outputs.
  """
  # Clone the node def
  node_def_ = op.to_node_def()

  # Transform name
  name_ = info.new_name(op.name)
  name_ = info.graph_.unique_name(name_)
  node_def_.name = name_

  # Transform names in colocation groups.
  if _COLOCATION_ATTR_NAME in node_def_.attr:
    cg_attr = node_def_.attr[_COLOCATION_ATTR_NAME]
    # Colocation group info is always a list of strings
    if cg_attr.list.s is None:
      raise ValueError("Invalid colocation group info '{}'".format(cg_attr))
    cg_attr_list = cg_attr.list.s
    for i in range(len(cg_attr_list)):
      old_cg_str = tf.compat.as_str(cg_attr_list[i])
      old_node_name = old_cg_str[len(_COLOCATION_PREFIX):]
      new_node_name = info.new_name(old_node_name)
      cg_attr_list[i] = tf.compat.as_bytes(_COLOCATION_PREFIX + new_node_name)

  # Mutate NodeDef if requested:
  if nodedef_fn is not None:
    node_def_ = nodedef_fn(node_def_)

  op_ = info.graph_.add_node_from_node_def(node_def_, set_inputs=False,
                                           set_control_inputs=False)
  # Input information in the NodeDef is ignored in favor of the provided new
  # set of inputs.
  op_.set_inputs(new_inputs)
  # Output type and shape information is not stored in the NodeDef.
  if copy_shape_and_dtype:
    op_.set_outputs_from_pairs([(t.dtype, t.shape) for t in op.outputs])
  else:
    op_.infer_outputs()

  return op_, op_.outputs


class TransformerInfo(object):
  """"Contains information about the result of a transform operation."""

  def __init__(self, info):
    """Constructor.

    Args:
      info: an instance of Transformer._TmpInfo containing various internal
        information about the transform operation.
    """
    self._graph = info.graph
    self._scope = info.scope
    self._graph_ = info.graph_
    self._scope_ = info.scope_
    self._transformed_ops = info.transformed_ops
    self._transformed_ts = info.transformed_ts

  def _get_transformed_map(self, top):
    """Return the correct container depending on the type of `top`."""
    if isinstance(top, Node):
      return self._transformed_ops
    elif isinstance(top, Tensor):
      return self._transformed_ts
    else:
      raise TypeError(
          "Expected a gde.Tensor or a gde.Node, got a {}".format(
              type(top)))

  def _transformed_elem(self, original_top, missing_fn=None):
    """Return the transformed op/tensor corresponding to the original one.

    Args:
      original_top: the original tensor/operation.
      missing_fn: function handling the case where the counterpart
        cannot be found. By default, None is returned.
    Returns:
      the transformed tensor/operation (or None if no match is found).
    """
    transformed_map = self._get_transformed_map(original_top)
    if isinstance(original_top, string_types):
      for original, transformed in iteritems(transformed_map):
        if original.name == original_top:
          return transformed
      return None if missing_fn is None else missing_fn(original_top)
    else:
      if original_top not in transformed_map:
        return None if missing_fn is None else missing_fn(original_top)
      return transformed_map[original_top]

  def _original_elem(self, transformed_top, missing_fn=None):
    """Return the original op/tensor corresponding to the transformed one.

    Args:
      transformed_top: the transformed tensor/operation.
      missing_fn: function handling the case where the counterpart
        cannot be found. By default, None is returned.
    Returns:
      the original tensor/operation (or None if no match is found).
    """
    transformed_map = self._get_transformed_map(transformed_top)
    check_name = isinstance(transformed_top, string_types)
    for original, transformed in iteritems(transformed_map):
      if ((check_name and transformed.name == transformed_top) or
              (not check_name and transformed == transformed_top)):
        return original
    return None if missing_fn is None else missing_fn(transformed_top)

  def transformed(self, original, missing_fn=None):
    """Return the transformed op/tensor corresponding to the original one.

    Note that the output of this function mimics the hierarchy
    of its input argument `original`.
    Given an iterable, it returns a list. Given an operation or a tensor,
    it will return an operation or a tensor.

    Args:
      original: the original tensor/operation.
      missing_fn: function handling the case where the counterpart
        cannot be found. By default, None is returned.
    Returns:
      the transformed tensor/operation (or None if no match is found).
    """
    transformed_elem = partial(self._transformed_elem, missing_fn=missing_fn)
    return util.transform_tree(original, transformed_elem)

  def original(self, transformed, missing_fn=None):
    """Return the original op/tensor corresponding to the transformed one.

    Note that the output of this function mimics the hierarchy
    of its input argument `transformed`.
    Given an iterable, it returns a list. Given an operation or a tensor,
    it will return an operation or a tensor.

    Args:
      transformed: the transformed tensor/operation.
      missing_fn: function handling the case where the counterpart
        cannot be found. By default, None is returned.
    Returns:
      the original tensor/operation (or None if no match is found).
    """
    original_elem = partial(self._original_elem, missing_fn=missing_fn)
    return util.transform_tree(transformed, original_elem)

  def __str__(self):
    res = StringIO()
    print("Transform result info:", file=res)
    if self._graph == self._graph_:
      in_place_str = "" if self._scope_ else " IN-PLACE"
      print("  Within graph[{}]{}".format(
          id(self._graph), in_place_str), file=res)
    else:
      print("  graph[{}] => graph[{}]".format(
          id(self._graph), id(self._graph_)), file=res)
    if self._scope:
      print("  Relative to source scope: {}".format(self._scope), file=res)
    if self._scope_:
      print("  Scope destination: {}".format(self._scope_), file=res)
    print("Operations mapping:", file=res)
    for op, op_ in iteritems(self._transformed_ops):
      print("  {} => {}".format(op.name, op_.name), file=res)
    return res.getvalue()


class _TmpInfo(object):
  """Transformer temporary data.

  An instance of this class holds all the information relevant to a call
  to a transformer instance (that is, a call to __call__). An instance
  is created for the life-time of the __call__ function and is passed as
  argument to the handlers.
  """

  def __init__(self,
               sgv,
               dst_graph, # type: Graph
               dst_scope,
               src_scope):
    self.sgv = sgv
    self.sgv_inputs_set = frozenset(sgv.inputs)
    self.ops = frozenset(sgv.ops)
    self.control_outputs = util.ControlOutputs(sgv.graph)
    self.graph = sgv.graph # type: Graph
    self.scope = src_scope
    self.graph_ = dst_graph
    self.scope_ = dst_scope
    self.transformed_ops = {}
    self.transformed_ts = {}
    self.collections = dict((key, self.graph.get_collection_by_name(key))
                            for key in self.graph.get_all_collection_keys())
    self.cyclic_ops = []
    self.transform_original_op_handler = transform_op_if_inside_handler
    # The graph is transformed op by op, in the same order the original ops
    # were created. However, this is sometimes not possible due to cycles
    # (i.e. while loops). So when the transformer creates a new op whose
    # inputs do not exist yet, temporary placeholders are created and stored
    # in this `tmp_cyclic_ts` container. During a second pass,
    # those temporary tensors are replaced by the proper transformed tensors
    # (see the function `_finalize_cycles`).
    self.tmp_cyclic_ts = []

  def new_name(self, name):
    """Compute a destination name from a source name.

    Args:
      name: the name to be "transformed".
    Returns:
      The transformed name.
    Raises:
      ValueError: if the source scope is used (that is, not an empty string)
        and the source name does not belong to the source scope.
    """
    scope = self.scope
    if not name.startswith(scope):
      raise ValueError("{} does not belong to source scope: {}.".format(
          name, scope))
    rel_name = name[len(scope):]
    name_ = self.scope_ + rel_name
    return name_


class Transformer(object):
  """Transform a subgraph into another one.

  By default, the constructor create a transform which copy a subgraph and
  replaces inputs with placeholders. This behavior can be modified by changing
  the handlers.
  """

  def __init__(self):
    """Transformer constructor.

    The following members can be modified:
    transform_op_handler: handle the transformation of a `gde.Node`.
      This handler defaults to a simple copy.
    assign_collections_handler: handle the assignment of collections.
      This handler defaults to assigning new collections created under the
      given name-scope.
    transform_external_input_handler: handle the transform of the inputs to
      the given subgraph. This handler defaults to creating placeholders
      instead of the ops just before the input tensors of the subgraph.
    transform_external_hidden_input_handler: handle the transform of the
      hidden inputs of the subgraph, that is, the inputs which are not listed
      in sgv.inputs. This handler defaults to a transform which keep the same
      input if the source and destination graphs are the same, otherwise
      use placeholders.
    transform_original_op_handler: handle the transform of original_op. This
      handler defaults to transforming original_op only if they are in the
      subgraph, otherwise they are ignored.
    """

    # handlers
    self.transform_op_handler = copy_op_handler
    self.transform_control_input_handler = transform_op_if_inside_handler
    self.assign_collections_handler = assign_renamed_collections_handler
    self.transform_external_input_handler = replace_t_with_placeholder_handler
    self.transform_external_hidden_input_handler = keep_t_if_possible_handler
    self.transform_original_op_handler = transform_op_if_inside_handler

  def __call__(self,
               sgv,
               dst_graph,
               dst_scope,
               src_scope="",
               reuse_dst_scope=False):
    """Execute the transformation.

    Args:
      sgv: the source subgraph-view.
      dst_graph: the destination graph.
      dst_scope: the destination scope.
      src_scope: the source scope, which specify the path from which the
        relative path of the transformed nodes are computed. For instance, if
        src_scope is a/ and dst_scoped is b/, then the node a/x/y will have a
        relative path of x/y and will be transformed into b/x/y.
      reuse_dst_scope: if True the dst_scope is re-used if it already exists.
        Otherwise, the scope is given a unique name based on the one given
        by appending an underscore followed by a digit (default).
    Returns:
      A tuple `(sgv, info)` where:
        `sgv` is the transformed subgraph view;
        `info` is an instance of TransformerInfo containing
        information about the transform, including mapping between
        original and transformed tensors and operations.
    Raises:
      ValueError: if the arguments are invalid.
    """
    sgv = subgraph.make_view(sgv)
    if not isinstance(dst_graph, Graph):
      raise TypeError("Expected a gde.Graph, got: {}".format(type(dst_graph)))

    src_scope = util.scope_finalize(src_scope)
    dst_scope = util.scope_finalize(dst_scope)

    # Potentially create new scope if reuse_dst_scope is False
    if dst_scope and not reuse_dst_scope:
      dst_scope = util.scope_finalize(dst_graph.unique_name(dst_scope[:-1]))

    # Create temporary info used during this transform call
    info = _TmpInfo(sgv, dst_graph, dst_scope, src_scope)

    self._copy_ops(info)
    self._finalize_cycles(info)
    self._connect_control_inputs(info)

    # Compute information about the transformation
    res_info = TransformerInfo(info)
    sgv_ = self._transform_sgv(info, sgv)
    return sgv_, res_info

  def _copy_ops(self, info):
    """Copy ops without connecting them."""
    sorted_ops = sorted(info.sgv.ops, key=lambda n: n.id_in_graph)
    for op in sorted_ops:
      new_inputs = [self._transformed_t(info, t, op) for t in op.inputs]
      op_, op_outputs_ = self.transform_op_handler(info, op, new_inputs)
      if op is op_:
        raise ValueError("In-place transformation not allowed.")

      # Process op.
      info.transformed_ops[op] = op_
      self.assign_collections_handler(info, op, op_)

      # Process output tensors.
      for op_output, op_output_ in zip(op.outputs, op_outputs_):
        info.transformed_ts[op_output] = op_output_
        self.assign_collections_handler(info, op_output, op_output_)

  def _finalize_cycles(self, info):
    """Reconnects the cyclic tensors."""
    for t, tmp_t_, consumer_op in info.tmp_cyclic_ts:
      if t not in info.transformed_ts:
        raise ValueError("The tensor {} should be transformed by now.".format(
            t.name))
      if consumer_op not in info.transformed_ops:
        raise ValueError("The op {} should be transformed by now.".format(
            consumer_op.name))
      t_ = info.transformed_ts[t]
      consumer_op_ = info.transformed_ops[consumer_op]
      t_index_ = list(consumer_op_.inputs).index(tmp_t_)
      consumer_op_.replace_input(t_index_, t_)

  def _connect_control_inputs(self, info):
    """Connect the previously copied ops."""
    for op in info.sgv.ops:
      tf.logging.debug("Connecting control inputs of op: %s", op.name)
      op_ = info.transformed_ops[op]

      # Finalize control inputs:
      control_inputs_ = [self.transform_control_input_handler(info, ci)
                         for ci in op.control_inputs]
      control_inputs_ = [ci for ci in control_inputs_ if ci is not None]
      op_.set_control_inputs(control_inputs_)

  def _transform_sgv(self, info, sgv):
    """Transform a subgraph view.

    For convenience, a transform operation returns a subgraph view of the
    transformed graph.

    Args:
      info: Temporary information for this transform call.
      sgv: the subgraph to be transformed.
    Returns:
      The transformed subgraph.
    """
    ops_ = [op_ for _, op_ in iteritems(info.transformed_ops)]
    sgv_ = subgraph.SubGraphView(ops_)
    sgv_inputs_ = sgv_.inputs
    sgv_outputs_ = sgv_.outputs

    # re-order inputs
    input_map_ = []
    for input_t in sgv.inputs:
      if input_t not in info.transformed_ts:
        continue
      input_t_ = info.transformed_ts[input_t]
      if input_t_ not in sgv_inputs_:
        continue
      input_t_index_ = sgv_.input_index(input_t_)
      input_map_.append(input_t_index_)

    # re-order outputs
    output_map_ = []
    for output_t in sgv.outputs:
      if output_t not in info.transformed_ts:
        continue
      output_t_ = info.transformed_ts[output_t]
      if output_t_ not in sgv_outputs_:
        continue
      output_t_index_ = sgv_.output_index(output_t_)
      output_map_.append(output_t_index_)

    return sgv_.remap(input_map_, output_map_)

  def _transformed_t(self, info, t, consumer_op):
    """Return tre transformed tensor of `t`."""
    if t in info.transformed_ts:
      # If op is in the subgraph, just return its transformed counterpart.
      return info.transformed_ts[t]

    if t in info.sgv_inputs_set:
      # `t` is an input of the subgraph.
      return self.transform_external_input_handler(info, t)
    elif t.node in info.ops:
      # `t` is an internal tensor but is not transformed yet because it
      # belongs to a graph cycle.
      tf.logging.debug("Cyclic tensor: t.name = %s", t.name)
      # Try to find an existing tensor we can use for now,
      # otherwise create one. We'll rewire this later.
      if consumer_op.op_type == "Merge":
        first_input = consumer_op.inputs[0]
        tmp_t_ = self._transformed_t(info, first_input, consumer_op)
      elif t.node.op_type == "Enter":
        enter_input = t.node.inputs[0]
        tmp_t_ = self._transformed_t(info, enter_input, consumer_op)
      else:
        tmp_t_ = util.make_placeholder_from_tensor(info.graph_, t,
                                                   scope=info.scope_,
                                                   prefix="geph_tmp")
        tf.logging.debug("Created temporary placeholder: %s.", tmp_t_.name)
      # Register as temporary and return.
      info.tmp_cyclic_ts.append((t, tmp_t_, consumer_op))
      return tmp_t_
    else:
      # `t` is a hidden input of the subgraph.
      return self.transform_external_hidden_input_handler(info, t)


def copy(sgv, dst_graph=None, dst_scope="", src_scope="",
         reuse_dst_scope=False):
  """Copy a subgraph.

  Args:
    sgv: the source subgraph-view. This argument is converted to a subgraph
      using the same rules than the function subgraph.make_view.
    dst_graph: the destination graph.
    dst_scope: the destination scope.
    src_scope: the source scope.
    reuse_dst_scope: if True the dst_scope is re-used if it already exists.
      Otherwise, the scope is given a unique name based on the one given
      by appending an underscore followed by a digit (default).
  Returns:
    A tuple `(sgv, info)` where:
      `sgv` is the transformed subgraph view;
      `info` is an instance of TransformerInfo containing
      information about the transform, including mapping between
      original and transformed tensors and operations.
  Raises:
    TypeError: if `dst_graph` is not a `gde.Graph`.
    StandardError: if sgv cannot be converted to a SubGraphView using
      the same rules than the function subgraph.make_view.
  """
  sgv = subgraph.make_view(sgv)
  if dst_graph is None:
    dst_graph = sgv.graph
  if not isinstance(dst_graph, Graph):
    raise TypeError("Expected a gde.Graph, got: {}".format(type(dst_graph)))

  copier = Transformer()
  return copier(
      sgv, dst_graph, dst_scope, src_scope, reuse_dst_scope=reuse_dst_scope)


def copy_with_input_replacements(sgv, replacement_ts,
                                 dst_graph=None, dst_scope="", src_scope="",
                                 reuse_dst_scope=False):
  """Copy a subgraph, replacing some of its inputs.

  Note a replacement only happens if the tensor to be replaced
  is an input of the given subgraph. The inputs of a subgraph can
  be queried using sgv.inputs.

  Args:
    sgv: the source subgraph-view. This argument is converted to a subgraph
      using the same rules as the function subgraph.make_view.
    replacement_ts: dictionary mapping from original tensors to the
      replaced one.
    dst_graph: the destination graph.
    dst_scope: the destination scope.
    src_scope: the source scope.
    reuse_dst_scope: if True the dst_scope is re-used if it already exists.
      Otherwise, the scope is given a unique name based on the one given
      by appending an underscore followed by a digit (default).
  Returns:
    A tuple `(sgv, info)` where:
      `sgv` is the transformed subgraph view;
      `info` is an instance of TransformerInfo containing
      information about the transform, including mapping between
      original and transformed tensors and operations.
  Raises:
    TypeError: if dst_graph is not a gde.Graph.
    StandardError: if sgv cannot be converted to a SubGraphView using
      the same rules as the function subgraph.make_view.
  """
  sgv = subgraph.make_view(sgv)
  if dst_graph is None:
    dst_graph = sgv.graph
  if not isinstance(dst_graph, Graph):
    raise TypeError("Expected a gde.Graph, got: {}".format(type(dst_graph)))

  copier = Transformer()

  # Replace tensor if possible.
  def replace_t_with_replacement_handler(info, t):
    if t in replacement_ts:
      return replacement_ts[t]
    else:
      return keep_t_if_possible_handler(info, t)
  copier.transform_external_input_handler = replace_t_with_replacement_handler
  return copier(
      sgv, dst_graph, dst_scope, src_scope, reuse_dst_scope=reuse_dst_scope)


def _add_control_flow_ops(ops, control_ios):
  """Complete `ops` so that the transformed graph is valid.

  Partially copying a graph can lead to a malformed graph. For instance,
  copying half of a while construct is likely to result in an invalid graph.
  This function attempts to add missing ops so that the transformation result
  in a valid graph.

  In addition to expanding with the contents of while loops, this function
  also adds any nodes that the target nodes expect to be collocated with.

  Args:
    ops: list of ops (modified in-place).
    control_ios: object created by a call to `util.ControlOutputs`.
  """
  g = ops[0].graph
  # Find frame names of while loops containing any of our ops.
  frame_names = set()
  for op in ops:
    frame_names.update(g.node_to_frame_names(op))
  # Find all ops in those while loops
  new_ops = set()
  for frame_name in frame_names:
    new_ops.update(g.frame_name_to_nodes(frame_name))
  # Add in any nodes that the target nodes expect to be collocated with.
  num_before = -1  # Always execute while loop below at least once.
  # Colocation groups may overlap. Repeat until we reach a fixed point.
  all_ops = new_ops.union(set(ops))
  while num_before != len(all_ops):
    num_before = len(all_ops)
    for group in g.colocation_groups.values():
      if len(all_ops.intersection(group)) > 0:
        all_ops.update(group)
  # Add any ops that weren't in the list before
  for op in all_ops.difference(ops):
    ops.append(op)


def _flatten_tree(tree, leaves=None):
  """Flatten a tree into a list.

  Args:
    tree: iterable or not. If iterable, its elements (child) can also be
      iterable or not.
    leaves: list to which the tree leaves are appended (None by default).
  Returns:
    A list of all the leaves in the tree.
  """
  if leaves is None:
    leaves = []
  if isinstance(tree, dict):
    for _, child in iteritems(tree):
      _flatten_tree(child, leaves)
  elif util.is_iterable(tree):
    for child in tree:
      _flatten_tree(child, leaves)
  else:
    leaves.append(tree)
  return leaves


# def _transform_tree(tree, fn, iterable_type=tuple):
#   """Transform all the nodes of a tree.
#
#   Args:
#     tree: iterable or not. If iterable, its elements (child) can also be
#       iterable or not.
#     fn: function to apply to each leaves.
#     iterable_type: type use to construct the resulting tree for unknown
#       iterable, typically `list` or `tuple`.
#   Returns:
#     A tree whose leaves has been transformed by `fn`.
#     The hierarchy of the output tree mimics the one of the input tree.
#   """
#   if util.is_iterable(tree):
#     if isinstance(tree, dict):
#       res = tree.__new__(type(tree))
#       res.__init__(
#           (k, _transform_tree(child, fn)) for k, child in iteritems(tree))
#       return res
#     elif isinstance(tree, tuple):
#       # NamedTuple?
#       if hasattr(tree, "_asdict"):
#         res = tree.__new__(type(tree), **_transform_tree(tree._asdict(), fn))
#       else:
#         res = tree.__new__(type(tree),
#                            (_transform_tree(child, fn) for child in tree))
#       return res
#     elif isinstance(tree, collections.Sequence):
#       res = tree.__new__(type(tree))
#       res.__init__(_transform_tree(child, fn) for child in tree)
#       return res
#     else:
#       return iterable_type(_transform_tree(child, fn) for child in tree)
#   else:
#     return fn(tree)


def graph_replace(target_ts, replacement_ts, dst_scope="",
                  src_scope="", reuse_dst_scope=False):
  """Create a new graph which computes the targets from the replaced Tensors.

  Args:
    target_ts: a single gde.Tensor or an iterable of gde.Tensor.
    replacement_ts: dictionary mapping from original tensors to replaced tensors
    dst_scope: the destination scope.
    src_scope: the source scope.
    reuse_dst_scope: if True the dst_scope is re-used if it already exists.
      Otherwise, the scope is given a unique name based on the one given
      by appending an underscore followed by a digit (default).
  Returns:
    A single gde.Tensor or a list of target gde.Tensor, depending on
    the type of the input argument `target_ts`.
    The returned tensors are recomputed using the tensors from replacement_ts.
  Raises:
    ValueError: if the targets are not connected to replacement_ts.
  """
  # Identify operations in the graph that will change.
  # Start forward walk at Tensors that will be replaced, and
  # backward walk at the target output Tensors.
  flatten_target_ts = _flatten_tree(target_ts)
  # Construct the forward control dependencies edges so that
  # the get_walks_intersection_ops can also traverse the
  # control dependencies.
  graph = util.get_unique_graph(flatten_target_ts, check_types=(Tensor,))
  control_ios = util.ControlOutputs(graph)
  ops = select.get_walks_intersection_ops(list(iterkeys(replacement_ts)),
                                          flatten_target_ts,
                                          control_ios=control_ios)
  if not ops:
    raise ValueError("Targets and replacements are not connected!")

  # Complete ops to avoid malformed control flow.
  # TODO(fkp): Consider moving this function deeper (in the transformer?).
  _add_control_flow_ops(ops, control_ios)

  # Create a copy of the relevant subgraph
  unused_sgv_, info = copy_with_input_replacements(
      ops, replacement_ts, None, dst_scope, src_scope, reuse_dst_scope)

  # Return the transformed targets but keep the original if the transformed
  # counterpart cannot be found
  def missing_fn(original_t):
    return original_t
  return info.transformed(target_ts, missing_fn)
