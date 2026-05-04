#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <DHT.h>

// === DISPLAYS ===
LiquidCrystal_I2C lcd(0x27, 16, 2);           // Change to 0x3F if needed

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// === PINS ===
#define VOLTAGE_PIN  A0
#define CURRENT_PIN  A1
#define DHT_PIN      2

// === DHT11 ===
#define DHTTYPE DHT11
DHT dht(DHT_PIN, DHTTYPE);

// === BATTERY PARAMETERS ===
const float UNDERVOLT_CUTOFF = 0.4;
const float OVERVOLT_CUTOFF  = 1;
const float MAX_DISCHARGE_A  = 5.6;
const float MAX_CHARGE_A     = 2.8;
const float SHORT_CIRCUIT_A  = 10.0;

// ACS712-05B
const float SENSITIVITY = 0.185;
const float VCC         = 5.0;
float OFFSET            = 2.5;

// Simple SOC
float calculateSOC(float v) {
  if (v >= 1) return 100.0;
  if (v <= 0.4) return 0.0;
  return ((v - 0.4) / (1 - 0.4)) * 100.0;
}

float readCurrent() {
  long sum = 0;
  for (int i = 0; i < 80; i++) {        // Reduced samples to save time/RAM
    sum += analogRead(CURRENT_PIN);
    delay(1);
  }
  float voltage = (sum / 80.0 / 1023.0) * VCC;
  return (voltage - OFFSET) / SENSITIVITY;
}

float readVoltage() {
  int raw = analogRead(VOLTAGE_PIN);
  return (raw / 1023.0) * VCC;   // Add voltage divider if >5V
}

void setup() {
  Serial.begin(9600);
  dht.begin();

  // LCD startup
  lcd.begin();
  lcd.backlight();
  lcd.print(F(" BMS"));
  lcd.setCursor(0, 1);
  lcd.print(F("Monitor Mode"));
  delay(2000);

  // OLED - Try both common addresses
  bool oledOk = false;
  if (display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    oledOk = true;
  } else if (display.begin(SSD1306_SWITCHCAPVCC, 0x3D)) {
    oledOk = true;
  }

  if (!oledOk) {
    Serial.println(F("SSD1306 allocation failed!"));
    Serial.println(F("Try: reduce code, use U8g2 lib, or bigger board (Mega)"));
    // Don't freeze - continue with LCD only
    lcd.clear();
    lcd.print(F("OLED Failed!"));
    lcd.setCursor(0,1);
    lcd.print(F("Check RAM/Wiring"));
  } else {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.println(F("OLED Ready"));
    display.display();
    delay(1000);
  }

  // Calibrate ACS712 (no current!)
  Serial.println(F("Calibrating ACS712..."));
  long sum = 0;
  for (int i = 0; i < 150; i++) {
    sum += analogRead(CURRENT_PIN);
    delay(4);
  }
  OFFSET = (sum / 150.0 / 1023.0) * VCC;

  Serial.print(F("OFFSET = "));
  Serial.println(OFFSET, 4);

  lcd.clear();
  lcd.print(F("Offset: "));
  lcd.print(OFFSET, 3);
  delay(1500);
  lcd.clear();

  Serial.println(F("BMS Monitor Ready (DHT11) "));
}

void loop() {
  float voltage = readVoltage();
  float current = readCurrent();
  float soc = calculateSOC(voltage);

  float temperature = dht.readTemperature();
  float humidity    = dht.readHumidity();
  if (isnan(temperature)) temperature = 0;
  if (isnan(humidity))    humidity = 0;

  // Status
  bool fault = false;
  const char* statusStr = "NORMAL";
  if (voltage < UNDERVOLT_CUTOFF)     { statusStr = "UNDERVOLT"; fault = true; }
  else if (voltage > OVERVOLT_CUTOFF) { statusStr = "OVERVOLT";  fault = true; }
  else if (abs(current) > SHORT_CIRCUIT_A) { statusStr = "SHORT"; fault = true; }
  else if (current > MAX_DISCHARGE_A)  { statusStr = "OVER D"; fault = true; }
  else if (current < -MAX_CHARGE_A)    { statusStr = "OVER C"; fault = true; }

  // === LCD ===
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(F("V:")); lcd.print(voltage, 2);
  lcd.print(F(" I:")); 
  if (current >= 0) lcd.print('+');
  lcd.print(current, 2); lcd.print('A');

  lcd.setCursor(0, 1);
  lcd.print(F("SOC:")); lcd.print(soc, 0); lcd.print(F("% T:"));
  lcd.print(temperature, 1); lcd.print('C');

  // === OLED (only if initialized) ===
  if (display.begin(SSD1306_SWITCHCAPVCC, 0x3C) || display.begin(SSD1306_SWITCHCAPVCC, 0x3D)) {  // quick re-check not needed but safe
    display.clearDisplay();

    // Battery body (inspired by clean radar style)
    display.drawRect(18, 12, 88, 38, SSD1306_WHITE);        // main body
    display.fillRect(104, 22, 10, 18, SSD1306_WHITE);       // terminal

    // SOC fill bar
    int barWidth = map(soc, 0, 100, 0, 82);
    display.fillRect(20, 14, barWidth, 34, SSD1306_WHITE);

    // Big SOC %
    display.setTextSize(2);
    display.setCursor(112, 18);
    display.print(soc, 0);
    display.setTextSize(1);
    display.print(F("%"));

    // Voltage & Current
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.print(F("V:")); display.print(voltage, 2);

    display.setCursor(0, 56);
    display.print(F("I:"));
    if (current >= 0) display.print('+');
    display.print(current, 2);
    display.print(F("A"));

    // Temp & Humidity
    display.setCursor(72, 0);
    display.print(F("T:")); display.print(temperature, 1); display.print('C');

    display.setCursor(72, 56);
    display.print(F("H:")); display.print(humidity, 0); display.print('%');

    // Status with warning style
    display.setCursor(2, 28);
    if (fault) {
      // Invert colors for faults to make them "pop"
      display.setTextColor(SSD1306_BLACK, SSD1306_WHITE);
      display.print(statusStr); // Displays "OVERVOLT", "SHORT", etc.
      display.setTextColor(SSD1306_WHITE);
    } else {
        display.print(statusStr); // Displays "NORMAL"
    }

    // Charging indicator
    if (current < -0.1) {
      display.setCursor(105, 48);
      display.print(F("CHG"));
    }

    display.display();
  }

  // Serial debug
  Serial.print(F("V:")); Serial.print(voltage, 3);
  Serial.print(F(" I:")); 
  if (current >= 0) Serial.print('+');
  Serial.print(current, 3);
  Serial.print(F(" SOC:")); Serial.print(soc, 1);
  Serial.print(F("% T:")); Serial.print(temperature, 1);
  Serial.print(F(" H:")); Serial.print(humidity, 0);
  Serial.print(F(" Status:")); Serial.println(statusStr);

  delay(900);   // ~1 Hz update (good for DHT11)
}