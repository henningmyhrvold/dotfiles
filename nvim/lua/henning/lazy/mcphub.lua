return {
  "ravitemer/mcphub.nvim",
  lazy = false,                      -- load on startup so keymaps work everywhere
  dependencies = { "nvim-lua/plenary.nvim" },
  config = function()
    local ok, mcphub = pcall(require, "mcphub")
    if not ok then
      vim.notify("mcphub.nvim not found", vim.log.levels.ERROR)
      return
    end

    -- Point mcphub at the Docker-backed MCP server
    mcphub.setup({
      -- This file tells mcphub how to launch/connect to the MCP server.
      -- We’ll create it in step 2 (or you may already have it from Ansible).
      config_path = vim.fn.expand("~/.config/mcp/mcphub.json"),
    })

    -- Keymaps (match what I suggested earlier)
    vim.keymap.set("n", "<leader>mo", function()
      mcphub.open()
    end, { desc = "MCP: Open Hub" })

    vim.keymap.set("n", "<leader>mt", function()
      mcphub.run_tool()
    end, { desc = "MCP: Run Tool" })

    vim.keymap.set("n", "<leader>mr", function()
      mcphub.insert_resource()
    end, { desc = "MCP: Insert Resource" })
  end,
}

