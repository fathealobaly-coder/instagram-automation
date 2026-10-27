      - name: Validate Python syntax
        run: |
          python -m py_compile trend_agent.py instagram_agent.py

      - name: Run Omni Monolith Engine
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          INSTAGRAM_ACCOUNT_ID: ${{ secrets.INSTAGRAM_ACCOUNT_ID }}
          INSTAGRAM_ACCESS_TOKEN: ${{ secrets.INSTAGRAM_ACCESS_TOKEN }}
          DRY_RUN: ${{ secrets.DRY_RUN }}
        run: python trend_agent.py
