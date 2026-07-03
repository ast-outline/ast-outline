defmodule MyApp.Router do
  @moduledoc "Distinct arities are distinct functions; clauses collapse."

  # Three distinct functions sharing a name but differing in arity —
  # all must surface.
  def route(conn), do: route(conn, [])
  def route(conn, opts) when is_list(opts), do: {conn, opts}
  def route(conn, opts, extra), do: {conn, opts, extra}

  # Two clauses of the same handle/1 — only the first survives dedup.
  def handle(:get), do: :get
  def handle(:post), do: :post
  def handle(_other), do: :unknown
end
