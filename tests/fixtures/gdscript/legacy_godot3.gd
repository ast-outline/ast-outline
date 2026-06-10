tool
extends Spatial

signal interacted(player)

export var title = "Lever"
export(int, 0, 10) var power = 5
onready var mesh = get_node("Mesh")

var health = 100 setget set_health, get_health

func set_health(value):
	health = value

func get_health():
	return health

master func request_action(action):
	pass

remote func sync_state(state):
	pass
