<template>
  <div class="counter">
    <h2>Counter: {{ count }}</h2>
    <button @click="increment">+</button>
    <button @click="decrement">-</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'

const count = ref(0)
const double = computed(() => count.value * 2)

function increment(): void {
  count.value++
}

function decrement(): void {
  count.value--
}
</script>

<style scoped>
.counter {
  text-align: center;
  padding: 2rem;
}

h2 {
  color: #42b883;
}

button {
  margin: 0 0.5rem;
  padding: 0.5rem 1rem;
  border: none;
  border-radius: 4px;
  background: #35495e;
  color: white;
  cursor: pointer;
}
</style>
