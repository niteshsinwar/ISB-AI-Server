import os
import logging
import json
import re
import ast
from typing import Dict, Any, List

from crewai import Agent, Task, Crew, Process
from langchain_google_genai import ChatGoogleGenerativeAI

# Import shared configurations
from app.config import GOOGLE_API_KEY, MODEL_TEXT_EXTRACTION

logger = logging.getLogger(__name__)

# Enhanced LLM Initialization with maximum performance settings
gemini_llm_document_crew = None
if GOOGLE_API_KEY:
    try:
        gemini_llm_document_crew = ChatGoogleGenerativeAI(
            model=MODEL_TEXT_EXTRACTION,
            temperature=0.1,  # Very low for maximum consistency and accuracy
            max_tokens=8192,  # Maximum context utilization
            top_p=0.8,  # Focused but not overly restrictive
            google_api_key=GOOGLE_API_KEY,
        )
        logger.info(f"Enhanced DocumentExtractionCrew LLM initialized with model: {MODEL_TEXT_EXTRACTION}")
    except Exception as e:
        logger.error(f"Failed to initialize enhanced LLM for DocumentExtractionCrew ({MODEL_TEXT_EXTRACTION}): {e}", exc_info=True)
        gemini_llm_document_crew = None
else:
    logger.critical("DOCUMENT_CREW: GOOGLE_API_KEY environment variable not set. LLM will not be available.")


class DocumentAnalysisAgentsProvider:
    """Provides super-intelligent agents for advanced document analysis with maximum Gemini utilization."""
    
    def master_query_deconstructor_agent(self) -> Agent:
        """
        Ultra-intelligent query analyzer that understands context, intent, and implicit requirements.
        """
        if not gemini_llm_document_crew:
            raise RuntimeError("LLM for Enhanced DocumentAnalysisAgentsProvider not initialized. Cannot create agent.")
        
        return Agent(
            role="Master Cognitive Query Architect & Semantic Intelligence Specialist",
            goal=(
                "You are an AI with superhuman query comprehension abilities. Your mission is to decode ANY user query "
                "with absolute precision and contextual intelligence. You must:\n"
                "1. EXTRACT ENTITIES: Identify every piece of information the user wants, preserving their EXACT terminology. "
                "Think beyond obvious keywords - understand implied requests, related fields, and contextual necessities.\n"
                "2. DOCUMENT TYPE INFERENCE: Use advanced pattern recognition to identify document types from minimal clues. "
                "Consider linguistic patterns, terminology clusters, and contextual hints that humans might miss.\n"
                "3. SEMANTIC ENRICHMENT: If the user's query seems incomplete or could benefit from related information extraction, "
                "intelligently expand the entity list with highly relevant fields that would provide complete context.\n"
                "4. INTELLIGENT ASSUMPTIONS: Make educated inferences about what the user truly needs, even if not explicitly stated.\n"
                "Your output must be a perfect JSON with 'entities_to_extract' and 'inferred_document_type'."
            ),
            backstory=(
                "You are the pinnacle of AI query understanding, trained on millions of document extraction scenarios. "
                "You possess an intuitive understanding of human information needs that surpasses typical AI limitations. "
                "You can read between the lines, understand context that isn't explicitly stated, and anticipate what "
                "information would be most valuable to extract. Your intelligence allows you to handle ambiguous requests, "
                "incomplete queries, and complex document relationships with ease. You never miss implicit requirements "
                "and always provide comprehensive entity lists that cover all aspects of what the user truly needs."
            ),
            llm=gemini_llm_document_crew,
            verbose=True,
            allow_delegation=False,
            max_iter=3,
            max_execution_time=120
        )

    def supreme_document_intelligence_agent(self) -> Agent:
        """
        The most advanced document extraction agent with superhuman analytical capabilities.
        """
        if not gemini_llm_document_crew:
            raise RuntimeError("LLM for Enhanced DocumentAnalysisAgentsProvider not initialized. Cannot create agent.")
        
        return Agent(
            role="Supreme Document Intelligence & Contextual Extraction Virtuoso",
            goal=(
                "You are an AI with extraordinary document comprehension that rivals and exceeds human experts. "
                "Your capabilities include:\n"
                "1. DEEP CONTEXTUAL ANALYSIS: Read documents like a human expert with years of experience in that domain.\n"
                "2. INTELLIGENT INFERENCE: When direct information isn't available, use contextual clues, calculations, "
                "cross-references, and logical deduction to derive accurate values.\n"
                "3. MULTI-LAYERED EXTRACTION: Look beyond surface text - analyze tables, headers, footers, watermarks, "
                "stamps, signatures, and any visual elements that contain information.\n"
                "4. ADAPTIVE REASONING: Adjust your extraction strategy based on document type, quality, format, and structure.\n"
                "5. CALCULATION & SYNTHESIS: Perform mathematical operations, date calculations, percentage computations, "
                "and data synthesis when needed to provide complete answers.\n"
                "6. CONTEXTUAL VALIDATION: Cross-check extracted information for consistency and logical coherence.\n"
                "7. PROBABILISTIC EXTRACTION: When 70-90% confident about information that's partially visible or inferable, "
                "provide it with appropriate clarification rather than marking as 'NOT FOUND'.\n"
                "Your goal is 90%+ success rate in finding requested information through any means necessary."
            ),
            backstory=(
                "You are the ultimate document analysis AI, combining the expertise of lawyers, accountants, researchers, "
                "administrative specialists, and data scientists. You have processed millions of documents across every "
                "domain - legal, financial, academic, government, medical, corporate, and personal. Your pattern recognition "
                "abilities allow you to understand document structures instantly. You can handle poor OCR, handwritten text, "
                "complex layouts, multiple languages, and corrupted data. You think like a detective, using every available "
                "clue to piece together information. You never give up easily and always explore multiple extraction strategies. "
                "Your analytical depth allows you to understand implicit relationships between data points and derive information "
                "that may not be explicitly stated but can be logically concluded from available evidence."
            ),
            llm=gemini_llm_document_crew,
            verbose=True,
            allow_delegation=False,
            max_iter=5,
            max_execution_time=300
        )

    def master_json_architect_agent(self) -> Agent:
        """
        Perfect JSON structuring agent with intelligent response formatting.
        """
        if not gemini_llm_document_crew:
            raise RuntimeError("LLM for Enhanced DocumentAnalysisAgentsProvider not initialized. Cannot create agent.")
        
        return Agent(
            role="Master JSON Architect & Intelligent Response Synthesizer",
            goal=(
                "Create perfect, intelligent JSON responses that provide maximum value to users. Your responsibilities:\n"
                "1. PERFECT STRUCTURE: Generate flawless JSON with exact entity names as keys.\n"
                "2. INTELLIGENT CLARIFICATION: Provide clarifications ONLY when truly necessary - for ambiguous data, "
                "significant assumptions, calculations performed, or confidence levels below 95%.\n"
                "3. VALUE OPTIMIZATION: Ensure every piece of extracted information is presented in its most useful form.\n"
                "4. CONFIDENCE COMMUNICATION: When providing inferred or calculated values, briefly explain the reasoning.\n"
                "5. NULL HANDLING: Use null values judiciously - only when information is genuinely unavailable after "
                "exhaustive analysis, not when it requires additional reasoning.\n"
                "Your JSON output must be the perfect final product that gives users exactly what they need."
            ),
            backstory=(
                "You are the final quality gate, ensuring that all the intelligence and hard work of previous agents "
                "is perfectly packaged for the user. You understand that users want answers, not excuses. You balance "
                "accuracy with usefulness, ensuring that inferred information is clearly marked but still provided when "
                "it adds value. You have perfect understanding of JSON structure and user experience principles. "
                "You never let technical perfection get in the way of providing useful information to users."
            ),
            llm=gemini_llm_document_crew,
            verbose=True,
            allow_delegation=False,
            max_iter=2,
            max_execution_time=60
        )


class DocumentAnalysisTasksProvider:
    """Provides enhanced task definitions with advanced prompt engineering."""
    
    def master_query_analysis_task(self, agent: Agent, user_prompt: str) -> Task:
        """
        Advanced query analysis task with intelligent entity expansion.
        """
        return Task(
            description=(
                f"MASTER QUERY ANALYSIS MISSION:\n"
                f"User Query: '{user_prompt}'\n\n"
                
                "COGNITIVE PROCESSING PROTOCOL:\n"
                "1. PRIMARY ANALYSIS:\n"
                "   - Extract EVERY explicitly requested entity with EXACT terminology preservation\n"
                "   - Identify implicit information needs that the user may not have articulated\n"
                "   - Consider what additional context would make the response more valuable\n\n"
                
                "2. DOCUMENT TYPE INTELLIGENCE:\n"
                "   - Analyze vocabulary patterns (e.g., 'marks', 'CGPA' → academic document)\n"
                "   - Look for domain-specific terminology (legal, financial, medical, administrative)\n"
                "   - Consider structural clues from the user's language\n"
                "   - Make educated inferences even from minimal information\n\n"
                
                "3. ENTITY ENRICHMENT LOGIC:\n"
                "   - If user asks for 'name', consider if they might also need 'full name', 'first name', 'last name'\n"
                "   - If user asks for 'date', determine if they need specific date formats or related dates\n"
                "   - For financial documents, consider related amounts, percentages, calculations\n"
                "   - For identity documents, consider verification numbers, issue dates, validity\n"
                "   - For academic documents, consider grades, percentages, ranks, institutions\n\n"
                
                "4. INTELLIGENT EXPANSION EXAMPLES:\n"
                "   - Query: 'get the salary' → Entities: ['salary', 'basic salary', 'gross salary', 'net salary']\n"
                "   - Query: 'show marks' → Entities: ['marks', 'percentage', 'grade', 'CGPA', 'total marks']\n"
                "   - Query: 'address details' → Entities: ['address', 'city', 'state', 'pincode', 'country']\n\n"
                
                "OUTPUT REQUIREMENTS:\n"
                "Return a JSON object with:\n"
                "- 'entities_to_extract': Array of strings (exact user terms + intelligent additions)\n"
                "- 'inferred_document_type': String (your best inference) or null\n\n"
                
                "CRITICAL SUCCESS FACTORS:\n"
                "- Preserve user's exact terminology while adding intelligent context\n"
                "- Never miss obvious document type clues\n"
                "- Think like a domain expert in the implied document type\n"
                "- Consider what information would be most useful to extract together\n\n"
                
                "Format: Final Answer: {JSON_OBJECT}"
            ),
            agent=agent,
            expected_output="A precise JSON object with comprehensive entity list and intelligent document type inference"
        )

    def supreme_extraction_task(self, agent: Agent, document_text_content: str) -> Task:
        """
        Ultimate document extraction task with maximum intelligence.
        """
        content_length = len(document_text_content)
        content_preview = document_text_content[:500] + "..." if content_length > 500 else document_text_content
        
        return Task(
            description=(
                "SUPREME DOCUMENT INTELLIGENCE EXTRACTION PROTOCOL:\n\n"
                
                f"DOCUMENT ANALYSIS TARGET:\n"
                f"Content Length: {content_length} characters\n"
                f"Content Preview: {content_preview}\n"
                f"Full Document Text:\n"
                "--- DOCUMENT START ---\n"
                f"{document_text_content}\n"
                "--- DOCUMENT END ---\n\n"
                
                "EXTRACTION INTELLIGENCE LEVELS:\n"
                
                "LEVEL 1 - DIRECT EXTRACTION:\n"
                "- Scan for exact matches of requested entities\n"
                "- Look in headers, bodies, tables, footnotes, signatures\n"
                "- Check different variations and formats\n\n"
                
                "LEVEL 2 - CONTEXTUAL REASONING:\n"
                "- Use surrounding text to understand context\n"
                "- Identify relationships between different data points\n"
                "- Handle abbreviations, acronyms, and alternative phrasings\n\n"
                
                "LEVEL 3 - INTELLIGENT INFERENCE:\n"
                "- Calculate derived values (e.g., age from DOB, totals from components)\n"
                "- Infer information from document structure and type\n"
                "- Use cross-references and validation\n\n"
                
                "LEVEL 4 - PROBABILISTIC EXTRACTION:\n"
                "- When 70%+ confident, provide best estimate with clarification\n"
                "- Use partial matches and context clues\n"
                "- Make educated guesses based on document patterns\n\n"
                
                "ADVANCED TECHNIQUES:\n"
                "1. MATHEMATICAL OPERATIONS:\n"
                "   - Sum components to get totals\n"
                "   - Calculate percentages, ratios, differences\n"
                "   - Derive dates from relative information\n\n"
                
                "2. PATTERN RECOGNITION:\n"
                "   - Identify standard document formats and their typical data locations\n"
                "   - Recognize government ID patterns, academic grading systems\n"
                "   - Understand business document structures\n\n"
                
                "3. MULTI-SOURCE SYNTHESIS:\n"
                "   - Combine information from different sections\n"
                "   - Resolve conflicts by using most authoritative source\n"
                "   - Build complete pictures from partial information\n\n"
                
                "4. QUALITY ASSESSMENT:\n"
                "   - Evaluate OCR quality and adapt strategy\n"
                "   - Handle poor formatting, misaligned text\n"
                "   - Work around damaged or incomplete sections\n\n"
                
                "DECISION MATRIX FOR EACH ENTITY:\n"
                "- FOUND CLEARLY: Extract exact value\n"
                "- FOUND WITH CALCULATION: Perform calculation and provide result\n"
                "- INFERABLE (70%+ confidence): Provide with clarification\n"
                "- PARTIALLY VISIBLE: Extract what's readable, note limitations\n"
                "- AMBIGUOUS: Choose most likely interpretation with explanation\n"
                "- GENUINELY NOT PRESENT: Mark as 'NOT FOUND' only after exhaustive search\n\n"
                
                "SUCCESS BENCHMARKS:\n"
                "- Aim for 90%+ entity resolution rate\n"
                "- Prefer intelligent inference over 'NOT FOUND'\n"
                "- Provide calculated/derived values when possible\n"
                "- Use domain expertise for each document type\n\n"
                
                "OUTPUT FORMAT:\n"
                "Final Answer:\n"
                "- **EXTRACTED DATA**:\n"
                "  Entity1: [Value/Calculation/Inference]\n"
                "  Entity2: [Value/NOT FOUND/AMBIGUOUS]\n"
                "- **EXTRACTION NOTES**: [Only for significant assumptions, calculations, or confidence < 95%]\n"
                "- **DOCUMENT QUALITY ASSESSMENT**: [Only if quality issues affect results]\n"
                "- **ERRORS ENCOUNTERED**: [Technical issues only]\n"
            ),
            agent=agent,
            expected_output=(
                "Comprehensive extraction results with maximum information recovery through intelligent analysis, "
                "calculations, inferences, and contextual reasoning. Minimal 'NOT FOUND' responses."
            )
        )

    def intelligent_json_synthesis_task(self, agent: Agent) -> Task:
        """
        Advanced JSON synthesis with intelligent value optimization.
        """
        return Task(
            description=(
                "MASTER JSON SYNTHESIS & INTELLIGENCE INTEGRATION:\n\n"
                
                "SYNTHESIS PROTOCOL:\n"
                "1. ENTITY MAPPING:\n"
                "   - Map each query entity to its extracted value\n"
                "   - Convert 'NOT FOUND'/'AMBIGUOUS' to null only when truly justified\n"
                "   - Preserve calculated and inferred values with their original form\n\n"
                
                "2. INTELLIGENT CLARIFICATION LOGIC:\n"
                "   - Include clarifications for:\n"
                "     * Calculated values (show the calculation)\n"
                "     * Inferred information (explain the reasoning)\n"
                "     * Confidence levels below 95%\n"
                "     * Document quality issues affecting results\n"
                "     * Significant assumptions made\n"
                "   - DO NOT clarify obvious, clearly visible data\n"
                "   - Keep clarifications concise but informative\n\n"
                
                "3. VALUE OPTIMIZATION:\n"
                "   - Present numbers in most useful format\n"
                "   - Standardize date formats when beneficial\n"
                "   - Clean up extracted text while preserving meaning\n"
                "   - Handle currency, percentages, and units appropriately\n\n"
                
                "4. USER EXPERIENCE FOCUS:\n"
                "   - Prioritize usefulness over technical perfection\n"
                "   - Provide actionable information\n"
                "   - Balance completeness with clarity\n"
                "   - Make intelligent decisions about what to include\n\n"
                
                "JSON STRUCTURE REQUIREMENTS:\n"
                "{\n"
                "  \"[Entity1]\": \"[Value/null]\",\n"
                "  \"[Entity2]\": \"[Value/null]\",\n"
                "  \"document_type\": \"[Type/null]\",\n"
                "  \"Clarification\": \"[Explanation/null]\",\n"
                "  \"Error\": \"[Error description/null]\"\n"
                "}\n\n"
                
                "CLARIFICATION EXAMPLES:\n"
                "- \"Age calculated as 25 based on DOB 1999-03-15 and current year 2024\"\n"
                "- \"Salary inferred as 50000 from 'Basic: 40000 + HRA: 10000' calculation\"\n"
                "- \"Address partially extracted due to poor OCR quality in bottom section\"\n"
                "- \"Grade derived from marks: 850/1000 = 85% = Grade A\"\n\n"
                
                "OUTPUT COMMAND:\n"
                "Your response must ONLY be: Final Answer: [JSON_OBJECT]\n"
                "No additional text, explanations, or formatting outside the JSON."
            ),
            agent=agent,
            expected_output="Perfect JSON object with optimized values and intelligent clarifications where beneficial"
        )


class DocumentExtractionCrewOrchestrator:
    """Enhanced orchestrator with maximum intelligence utilization."""
    
    def __init__(self, user_prompt: str, document_content: str):
        self.user_prompt = user_prompt
        self.document_content = document_content
        self.agents_provider = DocumentAnalysisAgentsProvider()
        self.tasks_provider = DocumentAnalysisTasksProvider()
        
        # Enhanced logging
        content_preview = self.document_content[:200].strip() + "..." if len(self.document_content) > 200 else self.document_content.strip()
        logger.info(f"Enhanced DocumentExtractionCrew initialized - Query: '{user_prompt}', Document Length: {len(document_content)}, Preview: '{content_preview}'")

    def run(self) -> Dict[str, Any]:
        """
        Execute the enhanced CrewAI workflow with maximum intelligence.
        """
        if not gemini_llm_document_crew:
            logger.error("Enhanced DocumentExtractionCrew cannot run: LLM not initialized.")
            return {
                "Error": "Enhanced AI model not available. Please check GOOGLE_API_KEY configuration.",
                "Clarification": "The advanced Gemini model required for intelligent document processing could not be loaded.",
                "document_type": None
            }

        try:
            logger.info("Initializing Enhanced DocumentExtractionCrew with supreme intelligence agents...")
            
            # Create enhanced agents
            query_master = self.agents_provider.master_query_deconstructor_agent()
            extraction_supreme = self.agents_provider.supreme_document_intelligence_agent()
            json_architect = self.agents_provider.master_json_architect_agent()

            logger.info("Defining advanced intelligence tasks...")
            
            # Create enhanced tasks
            query_task = self.tasks_provider.master_query_analysis_task(
                agent=query_master,
                user_prompt=self.user_prompt
            )

            extraction_task = self.tasks_provider.supreme_extraction_task(
                agent=extraction_supreme,
                document_text_content=self.document_content
            )
            extraction_task.context = [query_task]

            synthesis_task = self.tasks_provider.intelligent_json_synthesis_task(
                agent=json_architect
            )
            synthesis_task.context = [query_task, extraction_task]

            logger.info("Launching Enhanced DocumentExtractionCrew with maximum intelligence deployment...")
            
            # Create and run enhanced crew
            crew = Crew(
                agents=[query_master, extraction_supreme, json_architect],
                tasks=[query_task, extraction_task, synthesis_task],
                process=Process.sequential,
                verbose=2,  # Maximum verbosity for debugging
                max_rpm=10  # Allow intensive processing
            )

            final_result = crew.kickoff()
            logger.info(f"Enhanced DocumentExtractionCrew completed. Result preview: {str(final_result)[:300]}...")
            
            # Enhanced parsing with multiple fallback strategies
            expected_entities = self._extract_entities_from_query_result(query_task)
            return self._parse_enhanced_json_output(str(final_result), expected_entities)

        except Exception as e:
            logger.error(f"Enhanced DocumentExtractionCrew execution failed: {e}", exc_info=True)
            return {
                "Error": f"Enhanced AI processing failed: {str(e)}",
                "Clarification": "An error occurred during advanced document analysis. The system attempted maximum intelligence extraction but encountered technical difficulties.",
                "document_type": None,
                "extraction_attempted": True
            }

    def _extract_entities_from_query_result(self, query_task) -> List[str]:
        """Extract expected entities from query analysis for robust error handling."""
        try:
            if query_task and query_task.output and query_task.output.raw:
                raw_output = str(query_task.output.raw)
                json_match = re.search(r'Final Answer:\s*({.*?})', raw_output, re.DOTALL)
                if json_match:
                    query_result = json.loads(json_match.group(1))
                    return query_result.get("entities_to_extract", [])
        except Exception as e:
            logger.warning(f"Could not extract entities from query result: {e}")
        return []

    def _parse_enhanced_json_output(self, output_string: str, expected_entities: List[str]) -> Dict[str, Any]:
        """Enhanced JSON parsing with multiple fallback strategies."""
        logger.info(f"Parsing enhanced output (length: {len(output_string)})")
        
        if not output_string or not output_string.strip():
            logger.warning("Empty output received from enhanced crew")
            return self._create_fallback_response("Empty response from AI", expected_entities)

        # Multiple parsing strategies
        json_patterns = [
            r'Final Answer:\s*({.*})',  # Standard format
            r'(?:Final Answer:)?\s*({.*})',  # Flexible format
            r'({[^{}]*(?:{[^{}]*}[^{}]*)*})',  # Nested JSON pattern
        ]
        
        for pattern in json_patterns:
            matches = re.findall(pattern, output_string, re.DOTALL)
            for match in matches:
                try:
                    # Clean and parse JSON
                    clean_json = self._clean_json_string(match)
                    parsed_dict = json.loads(clean_json)
                    
                    if isinstance(parsed_dict, dict):
                        return self._structure_final_response(parsed_dict, expected_entities)
                        
                except json.JSONDecodeError:
                    continue

        # Final fallback: try ast.literal_eval
        try:
            # Extract potential dictionary from output
            dict_match = re.search(r'{[^{}]*(?:{[^{}]*}[^{}]*)*}', output_string, re.DOTALL)
            if dict_match:
                clean_text = self._clean_json_string(dict_match.group(0))
                parsed_dict = ast.literal_eval(clean_text)
                if isinstance(parsed_dict, dict):
                    return self._structure_final_response(parsed_dict, expected_entities)
        except Exception:
            pass

        logger.error(f"All parsing strategies failed. Raw output: {output_string[:500]}")
        return self._create_fallback_response("Failed to parse AI response", expected_entities, output_string[:1000])

    def _clean_json_string(self, json_str: str) -> str:
        """Clean JSON string for parsing."""
        # Remove markdown code blocks
        json_str = re.sub(r'```(?:json)?\s*', '', json_str, flags=re.IGNORECASE)
        json_str = re.sub(r'\s*```', '', json_str)
        
        # Remove extra whitespace and newlines
        json_str = json_str.strip()
        
        return json_str

    def _structure_final_response(self, parsed_dict: Dict[str, Any], expected_entities: List[str]) -> Dict[str, Any]:
        """Structure the final response with all required fields."""
        final_result = {}
        
        # Add expected entity keys
        for entity in expected_entities:
            final_result[entity] = parsed_dict.get(entity)
        
        # Add standard fields
        final_result['document_type'] = parsed_dict.get('document_type')
        final_result['Clarification'] = parsed_dict.get('Clarification')
        final_result['Error'] = parsed_dict.get('Error')
        
        # Add any additional fields from parsed result
        for key, value in parsed_dict.items():
            if key not in final_result:
                final_result[key] = value
        
        return final_result

    def _create_fallback_response(self, error_msg: str, expected_entities: List[str], raw_output: str = None) -> Dict[str, Any]:
        """Create structured fallback response."""
        response = {
            "Error": error_msg,
            "Clarification": "Enhanced AI processing encountered issues. Manual review may be required.",
            "document_type": None
        }
        
        # Add expected entity keys as null
        for entity in expected_entities:
            response[entity] = None
        
        if raw_output:
            response["raw_output_preview"] = raw_output
        
        return response