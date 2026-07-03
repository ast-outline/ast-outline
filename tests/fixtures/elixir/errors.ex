defmodule MyApp.NotFoundError do
  @moduledoc "Raised when a resource is missing."
  defexception [:message, plug_status: 404]

  @impl true
  def exception(opts) do
    %__MODULE__{message: Keyword.get(opts, :message, "not found")}
  end
end
