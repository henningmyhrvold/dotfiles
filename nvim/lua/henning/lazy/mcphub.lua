return {
  "mcphub.nvim",
  dependencies = { "nvim-lua/plenary.nvim" },
  lazy = false,

  config = function()
    local ok, hub = pcall(require, "mcphub")
    if ok and type(hub.setup) == "function" then
      hub.setup({
        cmd = "mcp-hub",            -- global install you've set up
        use_bundled_binary = false, -- don't try to use a bundled binary
        ui = { notify_on_ready = true },
      })
    end

    -- Minimal, stable mappings (no calls to non-existent functions)
    local function map(lhs, rhs, desc)
      vim.keymap.set("n", lhs, rhs, { desc = "MCP: " .. desc, silent = true })
    end

    -- Open the Hub UI (we’ll ensure it’s running via systemd below)
    map("<leader>mo", function()
      -- change the URL if you choose a different port in the systemd unit
      vim.ui.open("http://localhost:7801")
    end, "open hub UI")
  end,
}

