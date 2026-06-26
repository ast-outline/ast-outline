<template>
  <div class="options-api">
    <h2>{{ greeting }}</h2>
    <p>{{ message }}</p>
  </div>
</template>

<script lang="ts">
import { defineComponent } from 'vue'

export default defineComponent({
  name: 'OptionsComponent',
  data() {
    return {
      greeting: 'Hello',
      message: 'This uses the Options API.',
    }
  },
  computed: {
    fullMessage(): string {
      return `${this.greeting} - ${this.message}`
    },
  },
  methods: {
    greet(): void {
      console.log(this.fullMessage)
    },
  },
})
</script>

<style>
.options-api {
  font-family: sans-serif;
}
</style>
