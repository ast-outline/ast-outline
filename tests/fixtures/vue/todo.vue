<template>
  <div class="todo-app">
    <h1>{{ title }}</h1>
    <form @submit.prevent="addTodo">
      <input v-model="newTodo" placeholder="Add a todo" name="todo" />
      <button type="submit">Add</button>
    </form>
    <ul class="todo-list">
      <li v-for="todo in todos" :key="todo.id" class="todo-item">
        <span>{{ todo.text }}</span>
        <button @click="removeTodo(todo.id)">×</button>
      </li>
    </ul>
    <div v-if="todos.length === 0" class="empty-state">
      <p>No todos yet. Add one above.</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

interface Todo {
  id: number
  text: string
}

const title = ref('My Todo List')
const newTodo = ref('')
const todos = ref<Todo[]>([])

function addTodo(): void {
  if (!newTodo.value.trim()) return
  todos.value.push({
    id: Date.now(),
    text: newTodo.value.trim(),
  })
  newTodo.value = ''
}

function removeTodo(id: number): void {
  todos.value = todos.value.filter(t => t.id !== id)
}
</script>

<style scoped>
.todo-app {
  max-width: 600px;
  margin: 0 auto;
  padding: 2rem;
}
.todo-list {
  list-style: none;
  padding: 0;
}
.todo-item {
  display: flex;
  justify-content: space-between;
  padding: 0.5rem;
  border-bottom: 1px solid #eee;
}
.empty-state {
  text-align: center;
  color: #999;
  padding: 2rem;
}
</style>
