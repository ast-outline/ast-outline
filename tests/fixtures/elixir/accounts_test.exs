defmodule MyApp.AccountsTest do
  use ExUnit.Case, async: true

  alias MyApp.Accounts

  describe "get_user/1" do
    test "returns the user for a valid id" do
      import Ecto.Query
      assert Accounts.get_user(1)
    end

    test "returns nil for a missing id" do
      refute Accounts.get_user(-1)
    end
  end

  describe "policy" do
    test "reads are always allowed" do
      assert Accounts.Policy.can?(:read, nil)
    end
  end
end
