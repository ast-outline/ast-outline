# Exercises the KIND_BLOCK structural-callback rule (Ruby port of the
# issue #3 fix). Half the file is true-positive callback-DSL blocks; the
# other half is false-positive bait — calls that look superficially
# similar but must NOT be promoted to blocks.
require "rails_helper"

# --- True positives: callback-DSL blocks ---------------------------------

# Modern entry point is a member call (RSpec.describe). It is not itself
# a block, but transparent descent must surface everything inside it.
RSpec.describe User do
  # A named `let` — a labelled callback block, childless.
  let(:user) { build(:user) }

  describe "#full_name" do
    EXPECTED = "Ada Lovelace"

    it "returns the joined name" do
      expect(user.full_name).to eq(EXPECTED)
    end

    context "when the name is blank" do
      it "is empty" do
      end
    end
  end

  it("supports the brace block form") { expect(user).to be_valid }

  # A helper def inside a describe block — surfaces as a free function.
  def make_user(name)
    User.new(name: name)
  end
end

# Bare `describe` (no RSpec. prefix) — a plain-identifier container.
describe "bare describe suite" do
  it "still works" do
  end
end

# Rake-style nested tasks — symbol labels.
namespace :db do
  task :migrate do
    puts "migrating"
  end
end

# --- False positives: must NOT become blocks -----------------------------

# member-expression callee — not a plain identifier
File.open("config.yml") do |f|
  f.read
end

# label-less block — a wrapper, not a named container
loop do
  break
end

# plain-identifier call with a label but NO block — a plain DSL macro
gem "rspec-rails"

# no arguments at all — a label-less wrapper
configure do
  set :port, 4000
end

# --- Plain declarations alongside — must still be found ------------------

CALLBACKS_VERSION = "1.0"

class PlainClass
  def plain_method
  end
end

def plain_top_level_function
end
