extends Node2D

@export var spin_speed := 1.5

func _process(delta: float) -> void:
	rotation += spin_speed * delta

func reset() -> void:
	rotation = 0.0
