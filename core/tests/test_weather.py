from hannah.weather import WeatherCache
from hannah_proto.hannah_pb2 import AgentWeatherUpdate, WeatherCurrentData, WeatherForecastDay


def test_available_false_without_update():
    cache = WeatherCache()
    assert cache.available is False


def test_apply_update_and_build_answer_today():
    cache = WeatherCache()
    update = AgentWeatherUpdate(
        current=WeatherCurrentData(
            temperature=8.4,
            condition_detail="überwiegend bewölkt",
            precipitation_mm=0.0,
            wind_speed_ms=8.0,
            wind_direction_text="Westen",
        ),
        forecast=[
            WeatherForecastDay(day_offset=0, temperature_max=12.3),
        ],
    )
    cache.apply_update(update)

    assert cache.available is True
    answer = cache.build_answer(scope="today")
    assert "8 Grad" in answer
    assert "überwiegend bewölkt" in answer
    assert "12 Grad" in answer
    assert "Wind 29 km/h aus Westen" in answer


def test_build_answer_tomorrow():
    cache = WeatherCache()
    update = AgentWeatherUpdate(
        current=WeatherCurrentData(temperature=5.0),
        forecast=[
            WeatherForecastDay(
                day_offset=1,
                temperature_min=3.0,
                temperature_max=9.0,
                condition_detail="leichter Regen",
                precipitation_mm=1.5,
            ),
        ],
    )
    cache.apply_update(update)

    answer = cache.build_answer(scope="tomorrow")
    assert answer.startswith("Morgen, 3 bis 9 Grad, leichter regen.")
    assert "Regen erwartet" in answer


def test_build_answer_week():
    cache = WeatherCache()
    update = AgentWeatherUpdate(
        current=WeatherCurrentData(temperature=5.0),
        forecast=[
            WeatherForecastDay(day_offset=1, temperature_min=2.0, temperature_max=8.0, condition_detail="sonnig"),
            WeatherForecastDay(day_offset=2, temperature_min=1.0, temperature_max=6.0, condition_detail="sonnig"),
            WeatherForecastDay(day_offset=3, temperature_min=3.0, temperature_max=10.0, condition_detail="bewölkt"),
        ],
    )
    cache.apply_update(update)

    answer = cache.build_answer(scope="week")
    assert "1 bis 10 Grad" in answer
    assert "sonnig" in answer


def test_build_answer_today_missing_optional_fields():
    """current ohne Wind/Niederschlag (proto3 optional, nicht gesetzt) darf keine
    Wind-/Regen-Zusatzsätze erzeugen, statt None oder falsche Werte einzustreuen."""
    cache = WeatherCache()
    cache.apply_update(AgentWeatherUpdate(
        current=WeatherCurrentData(temperature=10.0, condition_detail="klar"),
    ))
    assert cache.build_answer(scope="today") == "Aktuell 10 Grad, klar."


def test_build_answer_today_without_data():
    cache = WeatherCache()
    assert cache.build_answer(scope="today") == "Ich habe leider keine aktuellen Wetterdaten."


def test_build_answer_tomorrow_without_forecast():
    cache = WeatherCache()
    cache.apply_update(AgentWeatherUpdate(current=WeatherCurrentData(temperature=5.0)))
    assert cache.build_answer(scope="tomorrow") == "Ich habe leider keine Vorhersage für morgen."


def test_apply_update_replaces_previous_snapshot():
    """Ein neues AgentWeatherUpdate ersetzt den kompletten Cache, statt alte Buckets stehen zu lassen."""
    cache = WeatherCache()
    cache.apply_update(AgentWeatherUpdate(
        current=WeatherCurrentData(temperature=5.0),
        forecast=[WeatherForecastDay(day_offset=1, temperature_max=9.0)],
    ))
    cache.apply_update(AgentWeatherUpdate(current=WeatherCurrentData(temperature=6.0)))

    assert cache.build_answer(scope="tomorrow") == "Ich habe leider keine Vorhersage für morgen."
