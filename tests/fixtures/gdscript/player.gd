## Player avatar — movement, health and inventory.
## Attach to the player scene root.
@icon("res://icons/player.svg")
class_name Player
extends CharacterBody2D

signal died(cause)
signal health_changed(old_value, new_value)

enum State { IDLE, RUNNING, JUMPING = 10, DEAD }
enum { FLAG_A, FLAG_B }

const MAX_SPEED: float = 300.0
const GRAVITY = 980
const BulletScene = preload("res://weapons/bullet.tscn")

@export var max_health: int = 100
@export_range(0.0, 1.0) var friction := 0.2
@onready var sprite: Sprite2D = $Body/Sprite2D
static var instance_count: int = 0

var _state: State = State.IDLE

## Current health. Clamped by the setter.
var health: int = 100:
	get:
		return health
	set(value):
		var old := health
		health = clampi(value, 0, max_health)
		health_changed.emit(old, health)

var speed_label: String:
	get = _get_speed_label

func _ready() -> void:
	instance_count += 1
	var bullet = BulletScene.instantiate()
	var fx = load("res://fx/spawn.tscn")
	add_child(bullet)
	add_child(fx.instantiate())

func _physics_process(delta: float) -> void:
	velocity.y += GRAVITY * delta
	move_and_slide()

## Applies damage, possibly emitting `died`.
func take_damage(amount: int, source: Node = null) -> void:
	health -= amount
	if health <= 0:
		died.emit("damage")

func _get_speed_label() -> String:
	return "%.1f" % velocity.length()

static func from_save(data: Dictionary) -> Player:
	var p := Player.new()
	p.health = data.get("health", 100)
	return p

func _init() -> void:
	_state = State.IDLE


class Inventory:
	## Items keyed by slot index.
	var _items: Dictionary = {}

	func add(slot: int, item: Resource) -> void:
		_items[slot] = item

	func count() -> int:
		return _items.size()
