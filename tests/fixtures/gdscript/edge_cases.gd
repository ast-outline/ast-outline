extends RefCounted

const FAKE_DECLS = """
func fake_function():
    pass
class FakeClass:
"""

const RAW_PATH = r"C:\game\func notes"
const SIG_NAME = &"signal_like"
const NODE_PATH = ^"/root/Main"

var multiline_call_result = compute(
    1,  # func in_comment(): ignored
    "func in_string(): ignored",
)

func compute(
    first: int,
    text: String = "default # not a comment",
) -> int:
    return first + text.length()

var with_continuation = 1 + \
    2

var callback := func(value): return value * 2

var named_lambda = func heavy(x):
    var local_inside_lambda = x
    return local_inside_lambda

func inline_body(): return 0

@abstract func must_override(amount: int) -> void

var typed_prop: int = 0:
    get:
        return typed_prop

var ref_prop: int: get = _read_only

var valued_ref = 0: get = _read_only, set = _write_only

func _read_only() -> int:
    return 1

func _write_only(value: int) -> void:
    pass

var a := 1; var b := 2

func tail() -> void:
    var scene = preload("res://late.tscn")
    add_child(scene.instantiate())

var banner = "Status   created {new_events}
    func not_a_decl():
    still inside the string"

signal after_banner

@export_enum ("Sphere", "Octahedron") var shape_kind: int
