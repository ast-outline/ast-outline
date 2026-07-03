defprotocol MyApp.Size do
  @moduledoc "Calculates the size of a data structure."

  @doc "Returns the size of `data`."
  def size(data)
end

defimpl MyApp.Size, for: BitString do
  alias MyApp.Helpers
  def size(str), do: byte_size(str)
end

defimpl MyApp.Size, for: Map do
  def size(map), do: map_size(map)
end
