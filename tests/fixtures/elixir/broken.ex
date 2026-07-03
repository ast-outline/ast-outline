defmodule MyApp.Broken do
  def valid(x), do: x + 1

  def dangling(x) do
    if x >
  # missing right-hand side and `end` below is unbalanced
end
