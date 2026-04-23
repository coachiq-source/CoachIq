<!--
  REPLACE THIS FILE WITH THE REAL MASTER SYSTEM PROMPT.

  The webhook server loads this file at startup and sends its contents as the
  system prompt when calling the Claude API. The server expects the model to
  return *two* HTML documents in its response, delimited either as fenced code
  blocks or with marker comments:

      <!-- ===== FULL PLAN START ===== -->
      ...html...
      <!-- ===== FULL PLAN END ===== -->

      <!-- ===== DECK SHEET START ===== -->
      ...html...
      <!-- ===== DECK SHEET END ===== -->

  (Both delimiter styles are supported by app/parser.py.)
-->

You are FirstWhistle, an expert water polo coach assistant. (Placeholder — Max
will replace this file with the full master system prompt before deploying.)
