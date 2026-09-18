# GridWise Improvement Plan


        try:
            response = await client.aio.models.generate_content(
                model=MODEL,
                contents=_build_user_message(operator_notes, battery),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=_RawResponse,
                ),
            )

