from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractionResult(BaseModel):
    source: str
    listing_url: str
    car: str | None = None
    car_full: str | None = None
    car_short: str | None = None
    vehicle_title: str | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    price_total_jpy: int | None = None
    vehicle_price_jpy: int | None = None
    price_total_source_text: str | None = None
    vehicle_price_source_text: str | None = None
    price_confidence: float = Field(default=0, ge=0, le=1)
    price_used_jpy: int | None = None
    price_used_type: str | None = None
    year: str | None = None
    mileage: str | None = None
    repair_history: str | None = None
    inspection: str | None = None
    dealer: str | None = None
    dealer_address: str | None = None
    dealer_business_hours: str | None = None
    dealer_closed_days: str | None = None
    dealer_website_url: str | None = None
    dealer_vehicle_url: str | None = None
    vin: str | None = None
    stock_number: str | None = None
    exterior_color: str | None = None
    interior_color: str | None = None
    fuel_type: str | None = None
    drivetrain: str | None = None
    transmission: str | None = None
    accident_history: str | None = None
    title_status: str | None = None
    owner_count: str | None = None
    recall_status: str | None = None
    seller_notes: str | None = None
    phone_from_listing: str | None = None
    carsensor_free_phone: str | None = None
    dealer_direct_phone: str | None = None
    extraction_confidence: float = Field(ge=0, le=1)
    missing_fields: list[str] = Field(default_factory=list)


class SpokenNormalizationResult(BaseModel):
    car_spoken_ru: str
    price_used_spoken_ru: str
    price_total_spoken_ru: str | None = None
    vehicle_price_spoken_ru: str | None = None
    year_spoken_ru: str | None = None
    mileage_spoken_ru: str | None = None
    inspection_spoken_ru: str | None = None


class CallAnalysisResult(BaseModel):
    available: bool | None = None
    price_confirmed: bool | None = None
    actual_price: str | None = None
    price_change_reason: str | None = None
    condition_notes: str | None = None
    seller_mood: str | None = None
    next_step: str | None = None
    final_summary_ru: str | None = None
    conclusion: str | None = None
    ai_quality_score: int | None = Field(default=None, ge=1, le=100)
    ai_quality_reason: str | None = None


class GoalGenerationResult(BaseModel):
    status: str = "ready"
    goal_ru: str | None = None
    target_vehicle: str | None = None
    main_intent: str | None = None
    constraints: list[str] = Field(default_factory=list)
    required_questions: list[str] = Field(default_factory=list)
    fallback_questions: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)


class RequestCallReportResult(BaseModel):
    call_status: str
    reached_sales: bool | None = None
    target_vehicle_or_task: str | None = None
    summary: str | None = None
    availability_result: str | None = None
    incoming_result: str | None = None
    price_result: str | None = None
    configuration_result: str | None = None
    vin_or_stock_result: str | None = None
    payment_result: str | None = None
    paperwork_result: str | None = None
    important_notes: str | None = None
    next_action: str | None = None
    ai_quality_score: int | None = Field(default=None, ge=1, le=100)
    ai_quality_reason: str | None = None


class RequestCallVehicleContext(BaseModel):
    source_url: str
    vehicle_title: str | None = None
    year: str | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    color: str | None = None
    power: str | None = None
    price: str | None = None
    mileage: str | None = None
    vin: str | None = None
    stock_number: str | None = None
    dealer_name: str | None = None
    dealer_phone: str | None = None
    dealer_address: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)


class DealerPhoneEvidence(BaseModel):
    source_url: str
    dealer_name_match: bool
    address_match: bool
    phone_found: str
    phone_label: str | None = None


class DealerPhoneResolutionResult(BaseModel):
    listing_url: str
    source: str | None = None
    dealer_name: str | None = None
    dealer_address: str | None = None
    dealer_business_hours: str | None = None
    listing_phone_raw: str | None = None
    listing_phone_type: str = "missing"
    resolved_phone_raw: str | None = None
    resolved_phone_e164: str | None = None
    resolved_phone_source_url: str | None = None
    source_type: str | None = None
    phone_type: str | None = None
    confidence_score: int = 0
    resolution_status: str
    evidence: list[DealerPhoneEvidence] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
    error_reason: str | None = None
