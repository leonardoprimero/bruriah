-- Bruriah Inline Archaeology for Neovim
-- Provides virtual text annotations, floating hover windows, and command integration.
local M = {}

M.config = {
  executable = "bruriah",
  enable_virtual_text = true,
  virtual_text_prefix = "🏛️ ",
  highlight_active = "Comment",
  highlight_stale = "DiagnosticWarn",
}

local ns = vim.api.nvim_create_namespace("bruriah_lens")

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", M.config, opts or {})

  -- Create user commands
  vim.api.nvim_create_user_command("BruriahWhy", function(args)
    local target = args.args ~= "" and args.args or (vim.fn.expand("%:p") .. ":" .. vim.fn.line("."))
    M.why(target)
  end, { nargs = "?" })

  vim.api.nvim_create_user_command("BruriahLens", function()
    M.render_lens()
  end, {})

  vim.api.nvim_create_user_command("BruriahUI", function()
    vim.fn.jobstart({ M.config.executable, "ui" }, { detach = true })
    vim.notify("🏛️ Bruriah UI launched in background", vim.log.levels.INFO)
  end, {})

  -- Autocommand to render lenses on BufReadPost, BufWritePost
  if M.config.enable_virtual_text then
    local group = vim.api.nvim_create_augroup("BruriahLensGroup", { clear = true })
    vim.api.nvim_create_autocmd({ "BufReadPost", "BufWritePost" }, {
      group = group,
      callback = function()
        M.render_lens()
      end,
    })
  end
end

function M.render_lens()
  local bufnr = vim.api.nvim_get_current_buf()
  local file_path = vim.api.nvim_buf_get_name(bufnr)
  if file_path == "" or vim.bo[bufnr].buftype ~= "" then
    return
  end

  vim.system(
    { M.config.executable, "lens", file_path, "--json" },
    { text = true },
    function(obj)
      if obj.code ~= 0 or not obj.stdout or obj.stdout == "" then
        return
      end

      local ok, data = pcall(vim.json.decode, obj.stdout)
      if not ok or not data or not data.lenses then
        return
      end

      vim.schedule(function()
        if not vim.api.nvim_buf_is_valid(bufnr) then
          return
        end
        vim.api.nvim_buf_clear_namespace(bufnr, ns, 0, -1)

        for _, lens in ipairs(data.lenses) do
          if lens.status ~= "unindexed" and lens.decision_title then
            local lnum = math.max(0, lens.line_start - 1)
            local text
            local hl
            if lens.status == "active" then
              text = string.format("%s%s (%s)", M.config.virtual_text_prefix, lens.decision_title, lens.commit_sha)
              hl = M.config.highlight_active
            else
              local rel = (lens.alert_relation or lens.status):upper()
              text = string.format("⚠️ %s by %s (%s)", rel, lens.successor_title or lens.successor_sha, lens.commit_sha)
              hl = M.config.highlight_stale
            end

            pcall(vim.api.nvim_buf_set_extmark, bufnr, ns, lnum, 0, {
              virt_text = { { text, hl } },
              virt_text_pos = "eol",
            })
          end
        end
      end)
    end
  )
end

function M.why(target)
  target = target or (vim.fn.expand("%:p") .. ":" .. vim.fn.line("."))
  vim.system(
    { M.config.executable, "why", target, "--json" },
    { text = true },
    function(obj)
      if obj.code ~= 0 or not obj.stdout or obj.stdout == "" then
        vim.schedule(function()
          vim.notify("Bruriah: No decision found for " .. target, vim.log.levels.WARN)
        end)
        return
      end

      local ok, data = pcall(vim.json.decode, obj.stdout)
      if not ok or not data then
        return
      end

      local lines = {}
      local dec = data.governing_decision
      if dec then
        table.insert(lines, "🏛️  " .. dec.subject)
        table.insert(lines, string.format("Commit: %s · Author: %s · Date: %s", dec.commit_sha:sub(1, 8), dec.author, dec.date))
        table.insert(lines, "")

        if data.lineage_alerts and #data.lineage_alerts > 0 then
          for _, alert in ipairs(data.lineage_alerts) do
            table.insert(lines, string.format("⚠️  ARCHITECTURAL DRIFT: %s by %s (%s)",
              alert.relation:upper(),
              alert.successor_subject or "Successor",
              alert.successor_commit and alert.successor_commit:sub(1, 8) or ""
            ))
          end
          table.insert(lines, "")
        end

        table.insert(lines, "--- Reasoning ---")
        for s in dec.body:gmatch("[^\r\n]+") do
          table.insert(lines, s)
        end
      else
        table.insert(lines, "Target: " .. data.target)
        table.insert(lines, "Commit: " .. (data.line_commit and data.line_commit.sha:sub(1, 8) or "unknown"))
        table.insert(lines, "No governing architectural decision recorded.")
      end

      vim.schedule(function()
        local buf = vim.api.nvim_create_buf(false, true)
        vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
        vim.bo[buf].filetype = "markdown"

        local width = math.min(80, vim.o.columns - 4)
        local height = math.min(#lines + 2, vim.o.lines - 4)
        local row = math.floor((vim.o.lines - height) / 2)
        local col = math.floor((vim.o.columns - width) / 2)

        vim.api.nvim_open_win(buf, true, {
          relative = "editor",
          width = width,
          height = height,
          row = row,
          col = col,
          style = "minimal",
          border = "rounded",
          title = " 🏛️ Bruriah Causal Archaeology ",
          title_pos = "center",
        })
      end)
    end
  )
end

return M
