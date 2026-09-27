"""redblue.video: trending-video creation from signals to reviewed renders.

Pipeline: trend sweep (official APIs + Tavily) → scoring → cited brief (Tavily + Firecrawl)
→ script → licensed/generated assets → FFmpeg render → human review. Other creators' videos
are never downloaded or reused; only metadata and public web pages are read.
"""
