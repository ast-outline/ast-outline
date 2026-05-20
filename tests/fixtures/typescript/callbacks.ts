// Exercises the KIND_BLOCK structural-callback rule (see issue #3).
// Half the file is true-positive callback-DSL blocks; the other half is
// false-positive bait — call expressions that look superficially similar
// but must NOT be promoted to blocks.
import { describe, it, expect } from 'vitest'
import { defineStore } from 'pinia'
import { ref } from 'vue'

// --- True positives: callback-DSL blocks ---------------------------------

describe('outer suite', () => {
  const sharedHelper = (x: number) => x * 2

  describe('nested suite', () => {
    it('case with locals', () => {
      const local = sharedHelper(21)
      expect(local).toBe(42)
    })
  })

  it('case with only assertions', () => {
    expect(true).toBe(true)
  })

  function factory(seed: number) {
    return seed + 1
  }
})

const useCounter = defineStore('counter', () => {
  const count = ref(0)
  function increment() {
    count.value++
  }
  return { count, increment }
})

export const useExportedStore = defineStore('exported', () => {
  const label = ref('x')
  return { label }
})

// --- False positives: must NOT become blocks -----------------------------

// callback present but trailing arg is a number, not the callback
setTimeout(() => {
  doSomething()
}, 1000)

// trailing array (react-hook deps shape) — callback is not last
registerEffect(() => {
  runEffect()
}, [depA, depB])

// member-expression callee, not a plain identifier
emitter.on('event', () => {
  handleEvent()
})

// no callback argument at all
console.log('startup message')

// string label present, but NOT first — a property-definition wrapper
// (Express's `defineGetter(obj, 'name', fn)` shape), not a container.
defineGetter(targetObject, 'computedValue', () => {
  return computeIt()
})

// plain-identifier callee + trailing callback, but NO string label:
// a bare function wrapper, not a named container — stays a field.
const wrapped = action((payload: string) => {
  const parsed = JSON.parse(payload)
  return parsed
})

// same wrapper shape but `export`-ed — the export path must also keep it
// a field and must NOT fabricate a block-style signature.
export const exportedWrapper = action((payload: string) => {
  return payload.trim()
})

// --- Plain declarations alongside — must still be found ------------------

export function plainFunction(): void {}

export class PlainClass {
  method(): void {}
}
