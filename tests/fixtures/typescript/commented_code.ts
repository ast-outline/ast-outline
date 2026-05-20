// Exercises `_is_commented_out_code` — a leading run of `//` comments
// that is disabled code must be dropped, genuine prose must be kept.

// A real one-line doc comment.
export function documented(): void {}

// const legacyImpl = createOldThing()
// configure(legacyImpl, {
//   retries: 3,
// })
export function afterDisabledCode(): void {}

// This explains the function below.
// The explanation runs over two prose lines.
export function twoLineProse(): void {}

// For each entry we compute a score.
// If the score is high enough, keep it.
export function proseStartingWithKeywords(): void {}

// Maps each raw key => its display label.
// Then folds label => a sorted position.
export function proseWithArrows(): void {}

/**
 * A JSDoc block is always documentation, never disabled code —
 * even though a heuristic might see code-shaped lines inside.
 */
export function jsDocumented(): void {}
