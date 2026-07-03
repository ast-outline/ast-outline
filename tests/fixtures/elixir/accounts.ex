defmodule MyApp.Accounts do
  @moduledoc """
  The Accounts context.
  """
  use GenServer
  import Ecto.Query, only: [from: 2]
  alias MyApp.{Repo, User}
  alias MyApp.Mailer
  require Logger

  defstruct [:id, :name, active: false, role: :member]

  @type t :: %__MODULE__{id: integer, name: String.t()}
  @typep state :: map
  @opaque token :: binary
  @type result(x) :: {:ok, x} | :error when x: var

  @callback fetch(id :: integer) :: {:ok, t} | :error
  @callback ready? :: boolean
  @callback merge(a :: t, b :: t) :: t when t: var

  defguard is_valid_id(x) when is_integer(x) and x > 0
  defguardp is_admin(role) when role == :admin

  @doc "Fetches a user by id, returning nil when absent."
  @spec get_user(integer) :: t | nil
  def get_user(id) when is_valid_id(id) do
    Repo.get(User, id)
  end

  def get_user(_id), do: nil

  defp normalize(name) do
    String.trim(name)
  end

  defmacro __using__(_opts) do
    quote do
      import MyApp.Accounts
    end
  end

  defmacrop debug_log(msg) do
    quote do: Logger.debug(unquote(msg))
  end

  defdelegate list_all(), to: Repo, as: :all

  defmodule Policy do
    @moduledoc "Authorization rules."

    def can?(:read, _user), do: true
    def can?(_, _), do: false
  end
end
